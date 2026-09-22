"""Broker-facing web app + JSON API + still-image ingestion.

Run:  uvicorn webapp.app:app --host 0.0.0.0 --port 8000
(or use run_webapp.bat)

- GET  /                      newest-first truck cards, searchable, click-to-call
- GET  /api/trucks            JSON list (q, source, limit, offset)
- GET  /api/trucks/{id}       JSON detail
- POST /api/trucks/report     mobile field report: 1-5 photos + fields -> one record
- POST /api/auth/register     mobile contributor self-registration (phone + password)
- POST /api/auth/login        mobile login -> bearer token
- GET  /api/auth/me           current account (bearer)
- POST /api/auth/logout       revoke the bearer session
- PATCH /api/trucks/{id}      telecaller review decision (reviewer auth)
- GET  /review                telecaller review queue (reviewer auth)
- GET/POST /review/login      telecaller web login (users table, session cookie)

Photos are never stored on the system — every source processes frames/images and
keeps only the extracted data.
"""
import hmac
import os
import re
import time
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import (
    Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import distinct, func, or_, select

from pipeline import auth
from pipeline.db import SourceType, Truck, User, get_session_factory, init_db
from pipeline.reports import _norm_plate
from webapp.broker_grouping import (
    find_lead_members, group_broker_rows, lead_phone, lead_plate, lead_trust,
)

_HERE = Path(__file__).parent

PAGE_SIZE = 15  # rows per page — keeps each page to ~one screen without long scroll

# `/` fetches+groups the *whole* filtered table on every request (see index()) —
# deliberately uncapped so grouping/paging never silently drops matches. That full
# fetch+group is the expensive part (DB query itself is sub-second even at 7k+ rows;
# most of the cost is materializing/grouping thousands of rows in Python). The prod
# VM is small and shared with other apps, so re-paying that cost on every single
# page view/refresh adds real load. These short-TTL caches let repeated requests
# for the same view reuse the last computed result instead of recomputing from
# scratch — correctness is unaffected (still zero data cap), just staleness up to
# the TTL, which is fine for a browsing/refresh workflow.
_INDEX_LEADS_TTL = 20   # seconds — filtered+grouped leads, keyed by filter params
_INDEX_FACETS_TTL = 60  # seconds — global type/city dropdown option lists
_index_leads_cache: dict = {}
_index_facets_cache: dict = {}

app = FastAPI(title="FreightDesk", description="Truck intelligence & dispatch platform")
templates = Jinja2Templates(directory=str(_HERE / "templates"))


def _phone_list(phone_str: Optional[str]) -> list:
    """Split a stored phone field ('9811008120; 9928001122') into call-ready entries:
    {raw, pretty (5+5 grouped), e164 (+91…)} so the UI can dial reliably and read easily."""
    out = []
    for part in (phone_str or "").split(";"):
        raw = part.strip()
        if not raw:
            continue
        digits = re.sub(r"\D", "", raw)
        last10 = digits[-10:] if len(digits) >= 10 else digits
        if len(last10) == 10:
            e164 = "+91" + last10
            pretty = f"{last10[:5]} {last10[5:]}"
        else:
            e164 = ("+" + digits) if digits else ""
            pretty = raw
        out.append({"raw": digits or raw, "pretty": pretty, "e164": e164})
    return out


templates.env.filters["phones"] = _phone_list


@app.on_event("startup")
def _startup():
    # Each step is isolated: a failure in one must never stop the next. They used to
    # share one try, so when ensure_seed_admin raised, start_worker() was skipped and
    # the OCR worker silently never ran — the site kept serving pages, so nothing
    # looked wrong while every mobile report sat QUEUED for 8 days. The worker is the
    # step that matters most, so it goes last but runs regardless.
    import logging
    log = logging.getLogger("freightdesk.startup")

    # Create the tables if they aren't there yet; browsing an empty DB still works.
    try:
        init_db()
    except Exception:
        log.exception("startup: init_db failed")
    # Make sure an admin exists so a fresh deploy is immediately usable.
    try:
        Session = get_session_factory()
        with Session() as s:
            auth.ensure_seed_admin(s)
            s.commit()
    except Exception:
        log.exception("startup: ensure_seed_admin failed")
    # Start the background OCR worker and recover any unfinished report jobs.
    try:
        from webapp import processing
        processing.start_worker()
    except Exception:
        log.critical("startup: OCR WORKER NOT STARTED - mobile reports will stay QUEUED",
                     exc_info=True)


# ── Auth: unified user accounts (mobile bearer + web cookie) ─────────────────────
# One `users` table backs both audiences. Contributors self-register via the mobile
# API; telecallers/admins are created by an admin (scripts/create_user.py). The
# review page + PATCH endpoint (which decide reward eligibility) require a
# telecaller/admin session. Keep everything behind HTTPS in production.

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
if not ADMIN_PASSWORD:
    ADMIN_PASSWORD = "admin"
    print("[webapp] WARNING: ADMIN_PASSWORD not set — using default 'admin'. "
          "Set ADMIN_PASSWORD before deploying.")

SESSION_COOKIE = "session"
# Send the Secure flag on the session cookie once HTTPS fronts the app.
SECURE_COOKIES = os.environ.get("SECURE_COOKIES", "0").lower() not in ("", "0", "false", "no")
SESSION_TTL_DAYS = 30
REVIEW_ROLES = {"telecaller", "admin"}

# Declared only so Swagger shows an "Authorize" (bearer) button on the mobile endpoints
# and sends the token. auto_error=False keeps it optional — the actual resolution still
# happens in get_current_user (which also honours the web session cookie).
bearer_scheme = HTTPBearer(
    auto_error=False,
    description="Paste the token returned by /api/auth/login or /api/auth/register.")

# Lightweight, session-detached identities passed around the request.
CurrentUser = namedtuple("CurrentUser", "id phone username display_name role")
Reviewer = namedtuple("Reviewer", "name user_id")


def _display_name(user) -> str:
    """Best human label for a user — display name, else their login id."""
    return user.display_name or user.username or user.phone


def _request_token(request: Request) -> Optional[str]:
    """Pull the login token from the request. An explicit Bearer header wins over the
    web session cookie, so an API client's token isn't shadowed by an ambient cookie."""
    authz = request.headers.get("Authorization", "")
    if authz.lower().startswith("bearer "):
        token = authz[7:].strip()
        if token:
            return token
    return request.cookies.get(SESSION_COOKIE) or None


def get_current_user(request: Request) -> Optional[CurrentUser]:
    """Resolve the logged-in account (cookie or bearer), or None."""
    token = _request_token(request)
    if not token:
        return None
    Session = get_session_factory()
    with Session() as s:
        u = auth.resolve_session(s, token)
        if not u:
            return None
        return CurrentUser(u.id, u.phone, u.username, u.display_name, u.role)


def _can_review(user: Optional[CurrentUser]) -> bool:
    return bool(user) and user.role in REVIEW_ROLES


def _legacy_admin_token_ok(value: str) -> bool:
    """Back-compat: the old shared ADMIN_PASSWORD via X-Admin-Token (automation only)."""
    return bool(value) and hmac.compare_digest(value, ADMIN_PASSWORD)


def require_reviewer(request: Request) -> Reviewer:
    """Dependency for review endpoints. Accepts a telecaller/admin session, or the
    legacy X-Admin-Token == ADMIN_PASSWORD for automation. 403 otherwise."""
    user = get_current_user(request)
    if _can_review(user):
        return Reviewer(_display_name(user), user.id)
    if _legacy_admin_token_ok(request.headers.get("X-Admin-Token") or ""):
        return Reviewer(ADMIN_USER, None)
    raise HTTPException(403, "Reviewer access required")


def require_admin(request: Request) -> Reviewer:
    """Dependency for user-account admin endpoints (list/approve users). Deliberately
    narrower than require_reviewer: role must be exactly 'admin', not 'telecaller' —
    account management is a different power than report review. Also accepts the
    legacy X-Admin-Token == ADMIN_PASSWORD for automation. 403 otherwise."""
    user = get_current_user(request)
    if user and user.role == "admin":
        return Reviewer(_display_name(user), user.id)
    if _legacy_admin_token_ok(request.headers.get("X-Admin-Token") or ""):
        return Reviewer(ADMIN_USER, None)
    raise HTTPException(403, "Admin access required")


def _nav_user(user: Optional[CurrentUser]) -> Optional[dict]:
    """Shape the logged-in user for the shared nav (_nav.html)."""
    if not user:
        return None
    return {"name": _display_name(user), "role": user.role,
            "can_review": _can_review(user)}


# ── Query helpers ────────────────────────────────────────────────────────────────

# Every text column the free-text search scans — type any word, match anywhere.
_SEARCH_COLS = (
    Truck.license_plate, Truck.company_name, Truck.phone_number, Truck.phone_reported,
    Truck.city, Truck.location, Truck.vehicle_type, Truck.other_text, Truck.website,
    Truck.reported_by, Truck.source_ref,
)


def _apply_search(stmt, q: Optional[str], source: Optional[str]):
    if q and q.strip():
        # Each typed word must appear in *some* field (AND across words, OR across
        # fields) so multi-word queries like "balaji jaipur" narrow naturally.
        for word in q.split():
            like = f"%{word}%"
            stmt = stmt.where(or_(*[col.ilike(like) for col in _SEARCH_COLS]))
    if source:
        stmt = stmt.where(Truck.source == source)
    return stmt


# Freshness buckets for the recency dot, and the optional time-window filter.
_FRESH_WINDOWS = {"24h": 1, "7d": 7, "30d": 30}  # label -> days


def _fresh_bucket(dt: Optional[datetime]) -> str:
    """new (<24h) / recent (<7d) / old — drives the row's recency dot colour."""
    if not dt:
        return "old"
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
    secs = (now - dt).total_seconds()
    if secs < 86400:
        return "new"
    if secs < 7 * 86400:
        return "recent"
    return "old"


def _fresh_cutoff(fresh: Optional[str]):
    """Lower bound on detected_at for the freshness filter, or None for All Time."""
    days = _FRESH_WINDOWS.get(fresh or "")
    return (datetime.now().astimezone() - timedelta(days=days)) if days else None


def _time_ago(dt: Optional[datetime]) -> str:
    if not dt:
        return ""
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
    # A few seconds "in the future" happens on essentially every fresh report (clock
    # skew between the reporting device and this server) — clamp to 0 so it reads
    # "just now" like any other brand-new row, instead of falling back to a raw
    # absolute timestamp that looks like a different column format.
    secs = max(0.0, (now - dt).total_seconds())
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= n:
            return f"{int(secs // n)}{unit} ago"
    return "just now"


# ── JSON API ─────────────────────────────────────────────────────────────────────

@app.get("/api/trucks")
def api_trucks(q: Optional[str] = None, source: Optional[str] = None,
               limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    Session = get_session_factory()
    with Session() as s:
        stmt = _apply_search(select(Truck), q, source)
        stmt = stmt.order_by(Truck.detected_at.desc()).limit(limit).offset(offset)
        rows = s.execute(stmt).scalars().all()
        return JSONResponse([r.as_dict() for r in rows])


@app.get("/api/trucks/{truck_id}")
def api_truck(truck_id: int):
    Session = get_session_factory()
    with Session() as s:
        row = s.get(Truck, truck_id)
        if not row:
            raise HTTPException(404, "Truck not found")
        return JSONResponse(row.as_dict())


def _ocr_plate_read(d: dict) -> Optional[str]:
    """What OCR actually read as a plate, for display — the dedicated plate-detector
    candidates if any were found, else the same digit-bearing body-text fragments
    pipeline/reports.py::_plate_fragments() draws on (that function itself isn't
    reusable here: it needs the live OCR dict, not a persisted trucks row)."""
    candidates = d.get("plate_candidates") or {}
    if candidates:
        return max(candidates, key=candidates.get)
    frags = []
    for t in (d.get("body_texts") or []):
        n = _norm_plate(t)
        if 2 <= len(n) <= 12 and any(c.isdigit() for c in n) and n not in frags:
            frags.append(n)
    return " / ".join(frags[:3]) if frags else None


def _plate_reason(d: dict) -> Optional[str]:
    """Full-sentence reason a field report's vehicle number is UNVERIFIED, for the
    detail panel's dedicated 'Reason' field (labeled and set apart from the plate
    there, so restating the typed number reads fine). Reconstructed from what IS
    persisted on the row: plate_confidence (the match category), license_plate (the
    typed number, in every category except OCR_ONLY/NONE), and the OCR read (see
    _ocr_plate_read) — worded as "OCR read", not "photo shows": OCR is noisy and can
    misread a plate the photo itself displays perfectly clearly, so implying the
    photo disagrees would be misleading. None for VERIFIED reports or non-field-
    report rows — nothing to explain there."""
    status = d.get("plate_confidence")
    if status == "MISMATCH":
        return f"Typed '{d.get('license_plate')}' — OCR read '{_ocr_plate_read(d) or '?'}' from the photo instead"
    if status == "REPORTED":
        return f"Typed '{d.get('license_plate')}' — OCR couldn't read a plate from the photo to confirm it"
    if status == "OCR_ONLY":
        return f"No number typed — '{d.get('license_plate')}' was read from the photo by OCR"
    if status == "NONE":
        return "No number typed, and OCR couldn't read a plate from the photo either"
    return None  # VERIFIED, or no plate_confidence recorded (video/stream)


def _plate_hover(d: dict) -> Optional[str]:
    """Short hover text for the 'i' icon review.html places right next to the
    already-shown plate — unlike _plate_reason(), doesn't restate the plate value
    itself (redundant right next to where it's already displayed)."""
    status = d.get("plate_confidence")
    if status == "MISMATCH":
        return f"OCR read '{_ocr_plate_read(d) or '?'}' from the photo instead"
    if status == "REPORTED":
        return "OCR couldn't read a plate from the photo to confirm this"
    if status == "OCR_ONLY":
        return "Not typed by the reporter — read from the photo by OCR"
    if status == "NONE":
        return "No number typed, and OCR couldn't read a plate from the photo either"
    return None


def _enrich(row: Truck) -> dict:
    d = row.as_dict()
    d["time_ago"] = _time_ago(row.detected_at)
    d["detected_at_human"] = row.detected_at.strftime("%Y-%m-%d %H:%M") if row.detected_at else ""
    d["reason"] = _plate_reason(d)
    d["plate_hover"] = _plate_hover(d)
    return d


@app.get("/trucks/{truck_id}/panel", response_class=HTMLResponse)
def truck_panel(request: Request, truck_id: int):
    """HTML fragment for the broker detail slide-over (loaded via HTMX on row click).

    This truck may be the representative of a grouped broker lead (see
    broker_grouping.py) — independently of whatever filters narrowed the list
    the user clicked from, look up every sibling report sharing its lead
    identity so the drawer can show full sighting history."""
    Session = get_session_factory()
    with Session() as s:
        row = s.get(Truck, truck_id)
        if not row:
            raise HTTPException(404, "Truck not found")
        d = _enrich(row)
        d["reviewed_at_human"] = (row.reviewed_at.strftime("%Y-%m-%d %H:%M")
                                  if row.reviewed_at else "")
        d["fresh"] = _fresh_bucket(row.detected_at)

        phone = lead_phone(d)
        plate = lead_plate(d)  # normalized + reliability-checked; only used if no phone
        candidate_stmt = None
        if phone:
            candidate_stmt = select(Truck).where(Truck.phone_number.ilike(f"%{phone}%"))
        elif plate:
            # Broad substring pre-filter on the RAW plate text (the stored column
            # isn't normalized) — find_lead_members() below refines it precisely.
            candidate_stmt = select(Truck).where(Truck.license_plate.ilike(f"%{d['license_plate']}%"))

        history = []
        if candidate_stmt is not None:
            candidates = [_enrich(c) for c in s.execute(candidate_stmt).scalars().all()]
            members = find_lead_members(candidates, truck_id)
            if len(members) > 1:
                history = sorted(members, key=lambda m: m.get("detected_at") or "", reverse=True)

    d["history"] = history
    return templates.TemplateResponse(request=request, name="detail_panel.html",
                                      context={"t": d})


# ── Mobile field-report ingestion ────────────────────────────────────────────────

@app.post("/api/trucks/report")
async def report(
    request: Request,
    images: List[UploadFile] = File(...),
    _creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    phone_number: Optional[str] = Form(None),
    vehicle_number: Optional[str] = Form(None),
    loaded_status: Optional[str] = Form(None),
    body_type: Optional[str] = Form(None),
    material_type: Optional[str] = Form(None),
    driver_name: Optional[str] = Form(None),
    number_of_wheels: Optional[int] = Form(None),
    axle_type: Optional[str] = Form(None),
    location: Optional[str] = Form(None),
    latitude: Optional[float] = Form(None),
    longitude: Optional[float] = Form(None),
    captured_at: Optional[str] = Form(None),
    reported_by: Optional[str] = Form(None),
):
    """Mobile app submission: 1-5 photos of ONE truck + form fields.

    ASYNC: the photos are stored and the report is accepted immediately (HTTP 202,
    processing_status=QUEUED). A background worker then OCRs the photos, reconciles
    them against the typed fields, and updates the row. The app polls
    GET /api/trucks/{id} until processing_status is DONE or FAILED.

    Photos are retained indefinitely (for OCR, telecaller review, abuse investigation,
    and re-processing with better models). Contributors are anonymous/paid,
    so a row becomes VERIFIED only when the photos confirm the typed vehicle number;
    otherwise it stays UNVERIFIED with a reason. Every submission is logged
    (submission_log) for abuse review.

    phone_number is optional — a report with none just has no callable contact number
    (same as a video/stream sighting that fails the require_phone gate), so a telecaller
    may not be able to act on it, but it's still stored and still goes through review.
    """
    import cv2
    import numpy as np
    from pipeline.image_api import MAX_IMAGES
    from pipeline.storage import get_storage
    from pipeline import db_writer
    from webapp import processing

    # This endpoint has no hard login requirement (see get_current_user use below), so an
    # inactive account (abuse-blocked, or a brand-new contributor still awaiting admin
    # approval — registration creates accounts with is_active=False) could otherwise just
    # drop its token and keep submitting anonymously. Check the typed phone itself against
    # the users table so is_active=False actually stops submissions either way. A blank
    # phone_number can't match any account, so this is a no-op when none is given.
    if phone_number and phone_number.strip():
        Session = get_session_factory()
        with Session() as s:
            inactive = auth.find_by_phone(s, phone_number)
            if inactive and not inactive.is_active:
                raise HTTPException(
                    403, "This account is not active yet — contact an admin")
    if not images:
        raise HTTPException(400, "At least one photo is required")
    if len(images) > MAX_IMAGES:
        raise HTTPException(400, f"At most {MAX_IMAGES} photos per truck")

    # Read + validate the uploads now (decoding is cheap; only the OCR is deferred).
    _ALLOWED_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
    uploads = []  # (bytes, ext, content_type)
    for up in images:
        data = await up.read()
        arr = np.frombuffer(data, dtype=np.uint8)
        if cv2.imdecode(arr, cv2.IMREAD_COLOR) is None:
            continue  # skip anything that isn't a decodable image
        ext = Path(up.filename or "").suffix.lower()
        if ext not in _ALLOWED_EXT:
            ext = ".jpg"
        uploads.append((data, ext, up.content_type or "image/jpeg"))
    if not uploads:
        raise HTTPException(400, "None of the uploads could be decoded as images")

    reported = {
        "vehicle_number": vehicle_number, "phone_number": phone_number,
        "loaded_status": loaded_status, "body_type": body_type,
        "material_type": material_type, "driver_name": driver_name,
        "number_of_wheels": number_of_wheels, "axle_type": axle_type,
        "location": location, "latitude": latitude, "longitude": longitude,
        "captured_at": captured_at, "reported_by": reported_by,
    }
    # If the contributor is logged in (bearer/cookie), attribute the report to that
    # account; otherwise keep the anonymous free-text reported_by. No hard auth needed.
    user = get_current_user(request)
    if user:
        reported["reported_by"] = _display_name(user)
        reported["reported_by_user_id"] = user.id
        reported["reporter_phone"] = user.phone

    # 1) Insert the QUEUED row from the typed fields (instant — no OCR here).
    row = db_writer.insert_pending_report(reported, images_count=len(uploads))
    truck_id = row["id"]
    # 2) Persist the photos (kept indefinitely), then attach their storage keys.
    #    Keys are "reports/<YYYY-MM-DD>/<truck_id>/<idx>" — the upload date FIRST, so a
    #    whole day is a single prefix. That shape is chosen for housekeeping: deleting
    #    or lifecycling one day's photos is then one folder in the console (or one
    #    `gcloud storage rm -r .../reports/<date>/`) instead of hand-picking hundreds of
    #    per-report folders. A busy day is ~40 reports/hour from a single contributor,
    #    so per-report folders at the top level get unmanageable fast.
    #    UTC, to match the objects' own timeCreated — a local-time label could read a
    #    day ahead of the metadata beside it. This is the *upload* date, deliberately
    #    not the client-supplied captured_at that detected_at uses.
    #    Nothing reads the key's shape: photos are always fetched via the image_keys
    #    stored on the row, so the two earlier layouts ("reports/<truck_id>/<idx>" and
    #    "reports/<truck_id>_<date>/<idx>") keep resolving untouched.
    storage = get_storage()
    stored_on = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    keys = []
    for idx, (data, ext, ctype) in enumerate(uploads):
        key = f"reports/{stored_on}/{truck_id}/{idx}{ext}"
        storage.put(key, data, content_type=ctype)
        keys.append(key)
    db_writer.set_image_keys(truck_id, keys)
    # 3) Hand off to the background worker and return immediately.
    processing.enqueue(truck_id)

    return JSONResponse(status_code=202, content={
        "id": truck_id,
        "processing_status": "QUEUED",   # poll status_url until DONE / FAILED
        "review_status": "PENDING",      # a telecaller reviews once processed
        "images_accepted": len(uploads),
        "status_url": f"/api/trucks/{truck_id}",
        "message": "Report accepted; OCR is running in the background.",
    })


@app.get("/report-test", response_class=HTMLResponse)
def report_test(request: Request):
    """Browser form to submit a field report — for testing before the mobile app
    is wired (Swagger UI can't do multi-file upload reliably)."""
    return templates.TemplateResponse(request=request, name="report_test.html", context={})


# ── Auth API (mobile contributors) ────────────────────────────────────────────────
# The mobile app's register/login screens call these. Registration only ever creates a
# `contributor`; telecaller/admin accounts are made by an admin via scripts/create_user.py.

class RegisterIn(BaseModel):
    phone: str
    password: str
    display_name: Optional[str] = None
    email: Optional[str] = None


class LoginIn(BaseModel):
    phone: str
    password: str


@app.post("/api/auth/register")
def api_register(body: RegisterIn):
    Session = get_session_factory()
    with Session() as s:
        try:
            # New contributors start inactive — an admin must approve them
            # (PATCH /api/admin/users/{id}) before the account can log in or the
            # bearer token below actually resolves to anything (resolve_session()
            # re-checks is_active on every request, so no separate step is needed
            # once approved — this same token just starts working).
            user = auth.create_user(
                s, body.password, phone=body.phone, display_name=body.display_name,
                email=body.email, role="contributor", registration_source="mobile",
                is_active=False)
        except auth.DuplicatePhone:
            raise HTTPException(409, "An account with this phone already exists")
        except ValueError as e:
            raise HTTPException(400, str(e))
        token = auth.create_session(s, user, ttl_days=SESSION_TTL_DAYS)
        out = user.as_dict()
        s.commit()
    return JSONResponse({"token": token, "user": out}, status_code=201)


@app.post("/api/auth/login")
def api_login(body: LoginIn):
    Session = get_session_factory()
    with Session() as s:
        user = auth.authenticate(s, body.phone, body.password)
        if not user:
            raise HTTPException(401, "Invalid phone or password")
        token = auth.create_session(s, user, ttl_days=SESSION_TTL_DAYS)
        out = user.as_dict()
        s.commit()
    return JSONResponse({"token": token, "user": out})


@app.get("/api/auth/me")
def api_me(request: Request,
           _creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    current = get_current_user(request)
    if not current:
        raise HTTPException(401, "Not authenticated")
    Session = get_session_factory()
    with Session() as s:
        u = s.get(User, current.id)
        return JSONResponse({"user": u.as_dict() if u else None})


@app.get("/api/auth/me/reports")
def api_my_reports(request: Request,
                   limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                   _creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    """The logged-in contributor's own submissions (newest first) + a status summary,
    so the app can show upload history and reward state. Only attributed reports appear —
    submissions made while logged out aren't tied to an account."""
    current = get_current_user(request)
    if not current:
        raise HTTPException(401, "Not authenticated")
    Session = get_session_factory()
    with Session() as s:
        mine = Truck.reported_by_user_id == current.id
        total = s.execute(select(func.count()).select_from(Truck).where(mine)).scalar_one()
        counts = dict(s.execute(
            select(Truck.review_status, func.count()).where(mine)
            .group_by(Truck.review_status)).all())
        rows = s.execute(select(Truck).where(mine).order_by(Truck.detected_at.desc())
                         .limit(limit).offset(offset)).scalars().all()
        reports = [r.as_dict() for r in rows]
    return JSONResponse({
        "total": total,
        "summary": {  # PASSED = reward-eligible
            "pending": counts.get("PENDING", 0),
            "passed": counts.get("PASSED", 0),
            "rejected": counts.get("REJECTED", 0),
        },
        "limit": limit, "offset": offset,
        "reports": reports,
    })


@app.post("/api/auth/logout")
def api_logout(request: Request,
               _creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme)):
    token = _request_token(request)
    Session = get_session_factory()
    with Session() as s:
        auth.delete_session(s, token)
        s.commit()
    return JSONResponse({"ok": True})


# ── Telecaller review: PATCH decision + queue page (reviewer auth) ─────────────────

_REVIEW_STATES = {"PENDING", "PASSED", "REJECTED"}


class ReviewPatch(BaseModel):
    review_status: str
    review_note: Optional[str] = None
    reviewed_by: Optional[str] = None


@app.patch("/api/trucks/{truck_id}")
def patch_truck(truck_id: int, body: ReviewPatch,
                reviewer: Reviewer = Depends(require_reviewer)):
    """Telecaller decision. review_status PASSED => contributor is reward-eligible."""
    status = (body.review_status or "").strip().upper()
    if status not in _REVIEW_STATES:
        raise HTTPException(400, f"review_status must be one of {sorted(_REVIEW_STATES)}")
    Session = get_session_factory()
    with Session() as s:
        row = s.get(Truck, truck_id)
        if not row:
            raise HTTPException(404, "Truck not found")
        row.review_status = status
        row.reviewed_by = (body.reviewed_by or "").strip() or reviewer.name
        row.reviewed_by_user_id = reviewer.user_id
        row.reviewed_at = datetime.now()
        row.review_note = (body.review_note or "").strip() or None
        s.commit()
        return JSONResponse(row.as_dict())


# ── Telecaller edit: correct a PENDING report's fields before deciding ─────────────

# What a reviewer may correct: the fields the contributor typed, plus company_name
# (OCR-filled, and a telecaller who has phoned the driver often knows it better).
# Value = max length, or None for the one integer field. Deliberately NOT editable:
# provenance (reported_by*, reporter_phone, phone_reported — the contributor's
# original number stays there even when phone_number is corrected), machine output
# (verification_status, plate_confidence, phone_ocr, plate_candidates, body_texts),
# GPS evidence (latitude/longitude), photos, and review_status (Pass/Reject owns it).
_EDITABLE_FIELDS = {
    "license_plate": 32, "phone_number": 128, "driver_name": 128, "company_name": 255,
    "loaded_status": 16, "body_type": 32, "material_type": 64, "axle_type": 32,
    "location": 255, "num_wheels": None,
}


class ReportEdit(BaseModel):
    # extra="forbid": a request trying to set anything outside the whitelist (say,
    # verification_status) is rejected outright rather than silently ignored.
    model_config = {"extra": "forbid"}
    license_plate: Optional[str] = None
    phone_number: Optional[str] = None
    driver_name: Optional[str] = None
    company_name: Optional[str] = None
    loaded_status: Optional[str] = None
    body_type: Optional[str] = None
    material_type: Optional[str] = None
    axle_type: Optional[str] = None
    location: Optional[str] = None
    num_wheels: Optional[int] = None


def _clean_edit_value(field: str, value):
    """Normalize one submitted value the way the report path stores it; blank -> None.
    Raises HTTPException(400) on a value that could not have come from a real report."""
    if field == "num_wheels":
        if value is None:
            return None
        if not 2 <= value <= 64:
            raise HTTPException(400, "num_wheels must be between 2 and 64")
        return value
    v = (value or "").strip()
    if not v:
        return None
    if field == "license_plate":
        v = re.sub(r"\s+", "", v).upper()
    elif field == "loaded_status":
        v = v.upper()
        if v not in ("LOADED", "UNLOADED"):
            raise HTTPException(400, "loaded_status must be LOADED or UNLOADED")
    elif field == "phone_number":
        # Same shape the report path stores: digits only, several numbers "; "-joined.
        parts = [re.sub(r"\D", "", p) for p in re.split(r"[;,/]", v)]
        parts = [p for p in parts if p]
        if not parts or any(len(p) < 10 for p in parts):
            raise HTTPException(400, "each phone number needs at least 10 digits")
        v = "; ".join(parts)
    limit = _EDITABLE_FIELDS[field]
    if limit and len(v) > limit:
        raise HTTPException(400, f"{field} is longer than {limit} characters")
    return v


@app.patch("/api/trucks/{truck_id}/fields")
def edit_report_fields(truck_id: int, body: ReportEdit,
                       reviewer: Reviewer = Depends(require_reviewer)):
    """Correct a mobile report's fields before a decision. Only fields present in the
    body are touched; every actual change is appended to edit_history.

    Refused (409) once the report is PASSED or REJECTED — the decision was made on the
    data as it stood, and PASSED is what makes the contributor reward-eligible — and
    while OCR is still QUEUED/PROCESSING, because the worker snapshots the typed fields
    when it starts and writes them all back when it finishes (finalize_report), which
    would silently overwrite an edit made in between."""
    submitted = body.model_dump(exclude_unset=True)
    if not submitted:
        raise HTTPException(400, "no fields to update")
    cleaned = {f: _clean_edit_value(f, v) for f, v in submitted.items()}

    Session = get_session_factory()
    with Session() as s:
        # Row lock: a concurrent Pass/Reject can't slip in between the status check
        # below and this write.
        row = s.get(Truck, truck_id, with_for_update=True)
        if not row:
            raise HTTPException(404, "Truck not found")
        if row.source != SourceType.image_api:
            raise HTTPException(400, "only mobile field reports can be edited")
        if (row.review_status or "PENDING") != "PENDING":
            raise HTTPException(409, f"report is already {row.review_status.lower()}; "
                                     "only pending reports can be edited")
        if row.processing_status in ("QUEUED", "PROCESSING"):
            raise HTTPException(409, "OCR is still processing this report; "
                                     "edit it once processing finishes")

        changes = {}
        for field, new in cleaned.items():
            old = getattr(row, field)
            if old != new:
                changes[field] = [old, new]
                setattr(row, field, new)
        if changes:
            entry = {"at": datetime.now(timezone.utc).isoformat(), "by": reviewer.name,
                     "by_user_id": reviewer.user_id, "changes": changes}
            # Reassign rather than append in place: a plain JSONB column doesn't
            # track in-place list mutation, so .append() would never be flushed.
            row.edit_history = list(row.edit_history or []) + [entry]
            s.commit()
        return JSONResponse(row.as_dict())


# ── User-account admin: list + approve/block (admin-only auth) ─────────────────────

@app.get("/api/admin/users")
def api_admin_list_users(is_active: Optional[bool] = None, role: Optional[str] = None,
                         limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                         _admin: Reviewer = Depends(require_admin)):
    """List user accounts, newest first. Filter with ?is_active=false to find accounts
    (typically contributors) awaiting approval, and/or ?role=contributor|telecaller|admin."""
    if role is not None and role not in auth.ROLES:
        raise HTTPException(400, f"role must be one of {auth.ROLES}")
    filters = []
    if is_active is not None:
        filters.append(User.is_active == is_active)
    if role is not None:
        filters.append(User.role == role)
    Session = get_session_factory()
    with Session() as s:
        total = s.execute(select(func.count()).select_from(User).where(*filters)).scalar_one()
        rows = s.execute(select(User).where(*filters).order_by(User.created_at.desc())
                         .limit(limit).offset(offset)).scalars().all()
        users = [u.as_dict() for u in rows]
    return JSONResponse({"total": total, "limit": limit, "offset": offset, "users": users})


class UserActivationPatch(BaseModel):
    is_active: bool


@app.patch("/api/admin/users/{user_id}")
def api_admin_patch_user(user_id: int, body: UserActivationPatch,
                         _admin: Reviewer = Depends(require_admin)):
    """Approve (is_active=true) or block (is_active=false) a user account. Blocking
    takes effect immediately: resolve_session()/authenticate() in pipeline/auth.py
    already refuse a disabled user, so their existing session/token stops working on
    its very next request without any separate revocation step."""
    Session = get_session_factory()
    with Session() as s:
        user = s.get(User, user_id)
        if not user:
            raise HTTPException(404, "User not found")
        user.is_active = body.is_active
        s.commit()
        return JSONResponse(user.as_dict())


@app.get("/trucks/{truck_id}/image/{idx}")
def truck_image(truck_id: int, idx: int, request: Request,
                user: Optional[CurrentUser] = Depends(get_current_user)):
    """Stream a stored report photo. Available to reviewers (telecaller/admin, for
    queue triage) and to the contributor who submitted the report (so the mobile app
    can show back what was uploaded); 403 otherwise. Photos are kept indefinitely, but
    ones uploaded before 2026-09-14 were deleted under the old ~2-day policy, so a 404
    here means the object is gone for good rather than temporarily unavailable."""
    import mimetypes
    from fastapi.responses import Response
    from pipeline.storage import get_storage
    Session = get_session_factory()
    with Session() as s:
        row = s.get(Truck, truck_id)
        if not row:
            raise HTTPException(404, "image not found")
        is_owner = bool(user) and row.reported_by_user_id is not None \
            and user.id == row.reported_by_user_id
        if not (_can_review(user) or is_owner
                or _legacy_admin_token_ok(request.headers.get("X-Admin-Token") or "")):
            raise HTTPException(403, "Not authorized to view this image")
        keys = row.image_keys or []
        if idx < 0 or idx >= len(keys):
            raise HTTPException(404, "image not found")
        key = keys[idx]
    data = get_storage().get(key)
    if data is None:
        raise HTTPException(404, "image no longer available")
    return Response(content=data, media_type=mimetypes.guess_type(key)[0] or "image/jpeg")


@app.get("/review/login", response_class=HTMLResponse)
def review_login_form(request: Request, error: Optional[str] = None):
    # Already signed in as a reviewer? Skip straight to the queue.
    if _can_review(get_current_user(request)):
        return RedirectResponse("/review", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html",
                                      context={"error": bool(error), "hide_signin": True})


@app.post("/review/login")
def review_login(request: Request, username: str = Form(...), password: str = Form(...)):
    Session = get_session_factory()
    with Session() as s:
        user = auth.authenticate(s, username, password)
        # The web app is for operators — only telecaller/admin accounts may sign in here.
        if not user or user.role not in REVIEW_ROLES:
            return RedirectResponse("/review/login?error=1", status_code=303)
        token = auth.create_session(s, user, ttl_days=SESSION_TTL_DAYS)
        s.commit()
    resp = RedirectResponse("/review", status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                    secure=SECURE_COOKIES, max_age=SESSION_TTL_DAYS * 86400)
    return resp


@app.get("/review/logout")
def review_logout(request: Request):
    token = _request_token(request)
    if token:
        Session = get_session_factory()
        with Session() as s:
            auth.delete_session(s, token)
            s.commit()
    resp = RedirectResponse("/review/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


def _pending_reports(s) -> int:
    """Count field reports awaiting telecaller review (for the nav badge)."""
    return s.execute(select(func.count()).select_from(Truck).where(
        Truck.source == SourceType.image_api,
        Truck.review_status == "PENDING")).scalar_one()


@app.get("/review", response_class=HTMLResponse)
def review(request: Request, review: str = "pending",
           verification: str = "all", loc: Optional[str] = None, q: Optional[str] = None,
           page: int = Query(1, ge=1)):
    user = get_current_user(request)
    if not _can_review(user):
        return RedirectResponse("/review/login", status_code=303)

    def _filt(stmt):
        stmt = stmt.where(Truck.source == SourceType.image_api)
        if review and review != "all":
            stmt = stmt.where(Truck.review_status == review.upper())
        if verification and verification != "all":
            stmt = stmt.where(Truck.verification_status == verification.upper())
        if loc:
            stmt = stmt.where(or_(Truck.city.ilike(loc), Truck.location.ilike(loc)))
        if q and q.strip():
            stmt = stmt.where(Truck.license_plate.ilike(f"%{q.strip()}%"))
        return stmt

    Session = get_session_factory()
    with Session() as s:
        total = s.execute(_filt(select(func.count()).select_from(Truck))).scalar_one()
        rows = s.execute(_filt(select(Truck)).order_by(Truck.detected_at.desc())
                         .limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)).scalars().all()
        pending_count = _pending_reports(s)
        # Location dropdown options — scoped to image_api cities only (the only
        # source this queue ever shows), so it never offers a value with zero results.
        cities = [c for (c,) in s.execute(
            select(distinct(Truck.city)).where(
                Truck.source == SourceType.image_api, Truck.city.isnot(None))
            .order_by(Truck.city)).all()]

    reports = [_enrich(r) for r in rows]

    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    return templates.TemplateResponse(request=request, name="review.html", context={
        "reports": reports, "review": review, "verification": verification,
        "loc": loc or "", "cities": cities, "q": q or "",
        "page": page, "pages": pages, "total": total, "user": _nav_user(user),
        "pending_count": pending_count,
    })


# ── Broker UI ────────────────────────────────────────────────────────────────────

# Whitelisted sort keys -> the lead-dict field each one sorts by (see
# broker_grouping.py). Default order is newest-first.
_LEAD_SORT_FIELDS = {
    "seen": "detected_at",
    "company": "company_name",
    "vehicle": "license_plate",
    "type": "vehicle_type",
    "source": "source",
}


def _sort_leads(leads: list, sort: str, descending: bool) -> list:
    field = _LEAD_SORT_FIELDS.get(sort, "detected_at")
    if field == "detected_at":
        # every row has one (NOT NULL) — plain direct sort
        return sorted(leads, key=lambda ld: ld.get("detected_at") or "", reverse=descending)
    # nulls-last regardless of direction, tie-broken newest-first — mirrors the
    # old SQL `(primary.nulls_last(), Truck.detected_at.desc())` ordering
    leads = sorted(leads, key=lambda ld: ld.get("detected_at") or "", reverse=True)
    with_val = [ld for ld in leads if ld.get(field)]
    without_val = [ld for ld in leads if not ld.get(field)]
    with_val.sort(key=lambda ld: ld.get(field), reverse=descending)
    return with_val + without_val


_TRUST_FILTERS = {"auto_verified", "verified", "pending", "rejected"}

# Columns index() actually needs — grouping identity (broker_grouping.py) + what
# index.html renders. Deliberately excludes everything else Truck carries (raw OCR
# audit fields incl. two JSONB blobs, review/processing metadata, coordinates, etc.)
# — those only matter in the per-lead detail slide-over, which already re-fetches
# its own full rows separately (see truck_panel()). Profiling on prod showed
# materializing full Truck ORM objects for the whole table is ~95% of index()'s
# cost, so selecting only these columns (plain Core Row tuples, no ORM instrumentation
# overhead, no JSONB decode) is the highest-leverage speedup available without
# capping or paginating the pre-group fetch (see the /-uncapped-fetch note above).
# If the broker page starts showing a new field, add its column here too.
_INDEX_LIST_COLUMNS = (
    Truck.id, Truck.detected_at, Truck.source, Truck.license_plate, Truck.company_name,
    Truck.phone_number, Truck.vehicle_type, Truck.city, Truck.location,
    Truck.loaded_status, Truck.body_type, Truck.material_type, Truck.driver_name,
    Truck.axle_type, Truck.other_text, Truck.review_status,
)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: Optional[str] = None, source: Optional[str] = None,
          vtype: Optional[str] = None, loc: Optional[str] = None,
          trust: str = "all", sort: str = "seen", dir: str = "desc",
          fresh: str = "all", page: int = Query(1, ge=1)):
    from urllib.parse import urlencode
    sort = sort if sort in _LEAD_SORT_FIELDS else "seen"
    descending = dir != "asc"
    cutoff = _fresh_cutoff(fresh)  # None for "all" (default — most data is historical)
    fresh = fresh if fresh in _FRESH_WINDOWS else "all"
    trust = trust if trust in _TRUST_FILTERS else "all"

    def _filtered(stmt):
        stmt = _apply_search(stmt, q, source)
        if cutoff is not None:
            stmt = stmt.where(Truck.detected_at >= cutoff)
        if vtype:
            stmt = stmt.where(Truck.vehicle_type == vtype)
        if loc:
            stmt = stmt.where(or_(Truck.city.ilike(loc), Truck.location.ilike(loc)))
        return stmt

    user = get_current_user(request)  # drives the shared nav (tabs + login/logout)
    can_review = _can_review(user)

    leads_key = (q or "", source or "", vtype or "", loc or "", fresh)
    now = time.time()
    cached_leads = _index_leads_cache.get(leads_key)
    cached_facets = _index_facets_cache.get("facets")

    Session = get_session_factory()
    with Session() as s:
        if cached_leads and now - cached_leads[0] < _INDEX_LEADS_TTL:
            leads = cached_leads[1]
        else:
            # All existing filters apply exactly as before, at the DB level (WHERE
            # clauses don't depend on what's in the SELECT list). What's still
            # different from a normal page: no LIMIT/OFFSET here — broker-lead
            # grouping (see broker_grouping.py) needs the whole filtered set in hand
            # before it can correctly cluster reports into leads and paginate those.
            rows = s.execute(_filtered(select(*_INDEX_LIST_COLUMNS))
                             .order_by(Truck.detected_at.desc())).all()
            reports = []
            for r in rows:
                d = {
                    "id": r.id,
                    "detected_at": r.detected_at.isoformat() if r.detected_at else None,
                    "source": r.source.value if isinstance(r.source, SourceType) else r.source,
                    "license_plate": r.license_plate,
                    "company_name": r.company_name,
                    "phone_number": r.phone_number,
                    "vehicle_type": r.vehicle_type,
                    "city": r.city,
                    "location": r.location,
                    "loaded_status": r.loaded_status,
                    "body_type": r.body_type,
                    "material_type": r.material_type,
                    "driver_name": r.driver_name,
                    "axle_type": r.axle_type,
                    "other_text": r.other_text,
                    "review_status": r.review_status,
                    "time_ago": _time_ago(r.detected_at),
                    "detected_at_human": r.detected_at.strftime("%Y-%m-%d %H:%M") if r.detected_at else "",
                    "fresh": _fresh_bucket(r.detected_at),
                }
                reports.append(d)
            leads = group_broker_rows(reports)
            # Sweep expired entries here (not on every request) so distinct search
            # queries don't accumulate in memory forever on this memory-constrained VM.
            for k, (ts, _) in list(_index_leads_cache.items()):
                if now - ts >= _INDEX_LEADS_TTL:
                    del _index_leads_cache[k]
            _index_leads_cache[leads_key] = (now, leads)

        if cached_facets and now - cached_facets[0] < _INDEX_FACETS_TTL:
            types, cities = cached_facets[1], cached_facets[2]
        else:
            types = [t for (t,) in s.execute(
                select(distinct(Truck.vehicle_type)).where(Truck.vehicle_type.isnot(None))
                .order_by(Truck.vehicle_type)).all()]
            cities = [c for (c,) in s.execute(
                select(distinct(Truck.city)).where(Truck.city.isnot(None))
                .order_by(Truck.city)).all()]
            _index_facets_cache["facets"] = (now, types, cities)

        pending_count = _pending_reports(s) if can_review else 0
    if trust != "all":
        leads = [ld for ld in leads if lead_trust(ld) == trust]
    leads = _sort_leads(leads, sort, descending)
    total = len(leads)  # leads, not raw reports — the correct unit once grouped
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, pages)
    trucks = leads[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]

    # Computed over the full filtered set (leads), not just this page's slice — otherwise
    # the column flickers in/out depending on which 15 rows land on the current page (e.g.
    # newest-first can put a run of video/stream sightings, which never carry location,
    # on page 1 even though plenty of field reports further back do).
    show_load = any(ld["loaded_status"] or ld.get("body_type") or ld.get("material_type") for ld in leads)
    show_location = any((ld.get("location") or ld.get("city")) for ld in leads)
    active_filters = sum(bool(x) for x in (q, source, vtype, loc)) \
        + (fresh != "all") + (trust != "all")
    # Filters only (no sort/page) — used to build sort/pagination links cleanly.
    base_qs = urlencode({"q": q or "", "source": source or "", "vtype": vtype or "",
                         "loc": loc or "", "fresh": fresh, "trust": trust})

    return templates.TemplateResponse(request=request, name="index.html", context={
        "trucks": trucks, "q": q or "", "source": source or "",
        "vtype": vtype or "", "loc": loc or "", "trust": trust,
        "types": types, "cities": cities, "active_filters": active_filters,
        "base_qs": base_qs,
        "sort": sort, "dir": "asc" if not descending else "desc", "fresh": fresh,
        "show_load": show_load, "show_location": show_location,
        "page": page, "pages": pages, "total": total,
        "user": _nav_user(user), "pending_count": pending_count,
    })
