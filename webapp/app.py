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
from collections import namedtuple
from datetime import datetime, timedelta
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
from pipeline.config import Config
from pipeline.db import SourceType, Truck, User, get_session_factory, init_db

_HERE = Path(__file__).parent

PAGE_SIZE = 15  # rows per page — keeps each page to ~one screen without long scroll

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
    # Create the tables if they aren't there yet; browsing an empty DB still works.
    try:
        init_db()
        # Make sure an admin exists so a fresh deploy is immediately usable.
        Session = get_session_factory()
        with Session() as s:
            auth.ensure_seed_admin(s)
            s.commit()
    except Exception as e:  # pragma: no cover - surfaced in logs
        print(f"[webapp] WARNING: could not reach the database: {e}")


# ── Lazy model bundle (only built when the image API is first hit) ───────────────

class _Models:
    def __init__(self):
        self.ready = False

    def load(self):
        if self.ready:
            return
        from pipeline.detector import Detector, PlateDetector
        from pipeline.ocr_engine import build_ocr_engine, FrameOCR
        cfg = Config()
        # Phone photos can be a touch soft — don't let the frame-blur gate zero them.
        cfg.blur_variance_threshold = 0.0
        self.detector = Detector(cfg)
        pd = PlateDetector()
        self.plate_detector = pd if pd.available() else None
        self.frame_ocr = FrameOCR(cfg, build_ocr_engine(cfg))
        self.ready = True


_models = _Models()


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
CurrentUser = namedtuple("CurrentUser", "id phone display_name role")
Reviewer = namedtuple("Reviewer", "name user_id")


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
        return CurrentUser(u.id, u.phone, u.display_name, u.role)


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
        return Reviewer(user.display_name or user.phone, user.id)
    if _legacy_admin_token_ok(request.headers.get("X-Admin-Token") or ""):
        return Reviewer(ADMIN_USER, None)
    raise HTTPException(403, "Reviewer access required")


def _nav_user(user: Optional[CurrentUser]) -> Optional[dict]:
    """Shape the logged-in user for the shared nav (_nav.html)."""
    if not user:
        return None
    return {"name": user.display_name or user.phone, "role": user.role,
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
    secs = (now - dt).total_seconds()
    if secs < 0:
        return dt.strftime("%Y-%m-%d %H:%M")
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


@app.get("/trucks/{truck_id}/panel", response_class=HTMLResponse)
def truck_panel(request: Request, truck_id: int):
    """HTML fragment for the broker detail slide-over (loaded via HTMX on row click)."""
    Session = get_session_factory()
    with Session() as s:
        row = s.get(Truck, truck_id)
        if not row:
            raise HTTPException(404, "Truck not found")
        d = row.as_dict()
        d["time_ago"] = _time_ago(row.detected_at)
        d["detected_at_human"] = (row.detected_at.strftime("%Y-%m-%d %H:%M")
                                  if row.detected_at else "")
        d["reviewed_at_human"] = (row.reviewed_at.strftime("%Y-%m-%d %H:%M")
                                  if row.reviewed_at else "")
        d["fresh"] = _fresh_bucket(row.detected_at)
    return templates.TemplateResponse(request=request, name="detail_panel.html",
                                      context={"t": d})


# ── Mobile field-report ingestion ────────────────────────────────────────────────

@app.post("/api/trucks/report")
async def report(
    request: Request,
    images: List[UploadFile] = File(...),
    phone_number: str = Form(...),
    _creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    vehicle_number: Optional[str] = Form(None),
    loaded_status: Optional[str] = Form(None),
    number_of_wheels: Optional[int] = Form(None),
    location: Optional[str] = Form(None),
    latitude: Optional[float] = Form(None),
    longitude: Optional[float] = Form(None),
    captured_at: Optional[str] = Form(None),
    reported_by: Optional[str] = Form(None),
):
    """Mobile app submission: 1-5 photos of ONE truck + form fields -> one DB row.

    Photos are OCR'd (never stored). Contributors are anonymous/paid, so the row is
    VERIFIED only when the photos confirm the typed vehicle number; otherwise it's
    stored UNVERIFIED with a reason. Every submission is logged (submission_log) for
    abuse review. The phone is merged with any OCR-read phone.
    """
    import cv2
    import numpy as np
    from pipeline.image_api import build_event_from_images, MAX_IMAGES
    from pipeline.extract import extract_truck_fields
    from pipeline.reports import reconcile
    from pipeline.db_writer import insert_report

    if not phone_number or not phone_number.strip():
        raise HTTPException(400, "phone_number is required")
    if not images:
        raise HTTPException(400, "At least one photo is required")
    if len(images) > MAX_IMAGES:
        raise HTTPException(400, f"At most {MAX_IMAGES} photos per truck")

    decoded = []
    for up in images:
        data = await up.read()
        arr = np.frombuffer(data, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is not None:
            decoded.append(img)
    if not decoded:
        raise HTTPException(400, "None of the uploads could be decoded as images")

    _models.load()
    # Build the OCR view of the truck — images are processed, never saved.
    event = build_event_from_images(decoded, _models.detector, _models.plate_detector,
                                    _models.frame_ocr, capture_crop=False)
    ocr = extract_truck_fields(event) or {}

    reported = {
        "vehicle_number": vehicle_number, "phone_number": phone_number,
        "loaded_status": loaded_status, "number_of_wheels": number_of_wheels,
        "location": location, "latitude": latitude, "longitude": longitude,
        "captured_at": captured_at, "reported_by": reported_by,
    }
    # Transition mode: if the contributor is logged in (bearer/cookie), attribute the
    # report to that account and snapshot their name/phone; otherwise keep today's
    # anonymous behavior (free-text reported_by from the form). No hard auth required.
    user = get_current_user(request)
    if user:
        reported["reported_by"] = user.display_name or user.phone
        reported["reported_by_user_id"] = user.id
        reported["reporter_phone"] = user.phone

    # Always stored; the photos decide whether it's VERIFIED or UNVERIFIED.
    fields = reconcile(reported, ocr, Config())
    row = insert_report(fields, images_count=len(decoded))
    return JSONResponse({
        "truck": row,
        "images_processed": len(decoded),
        "verification_status": fields["verification_status"],
        "review_status": row.get("review_status"),  # PENDING until a telecaller reviews
        "reason": fields["reason"],
        "plate_status": fields["plate_status"],
        "phone_status": fields["phone_status"],
        "reported_by_user_id": row.get("reported_by_user_id"),
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
            user = auth.create_user(
                s, body.phone, body.password, display_name=body.display_name,
                email=body.email, role="contributor", registration_source="mobile")
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


@app.get("/review/login", response_class=HTMLResponse)
def review_login_form(request: Request, error: Optional[str] = None):
    # Already signed in as a reviewer? Skip straight to the queue.
    if _can_review(get_current_user(request)):
        return RedirectResponse("/review", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html",
                                      context={"error": bool(error)})


@app.post("/review/login")
def review_login(request: Request, phone: str = Form(...), password: str = Form(...)):
    Session = get_session_factory()
    with Session() as s:
        user = auth.authenticate(s, phone, password)
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
           verification: str = "all", page: int = Query(1, ge=1)):
    user = get_current_user(request)
    if not _can_review(user):
        return RedirectResponse("/review/login", status_code=303)

    def _filt(stmt):
        stmt = stmt.where(Truck.source == SourceType.image_api)
        if review and review != "all":
            stmt = stmt.where(Truck.review_status == review.upper())
        if verification and verification != "all":
            stmt = stmt.where(Truck.verification_status == verification.upper())
        return stmt

    Session = get_session_factory()
    with Session() as s:
        total = s.execute(_filt(select(func.count()).select_from(Truck))).scalar_one()
        rows = s.execute(_filt(select(Truck)).order_by(Truck.detected_at.desc())
                         .limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)).scalars().all()
        pending_count = _pending_reports(s)

    reports = []
    for r in rows:
        d = r.as_dict()
        d["time_ago"] = _time_ago(r.detected_at)
        d["detected_at_human"] = r.detected_at.strftime("%Y-%m-%d %H:%M") if r.detected_at else ""
        reports.append(d)

    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    return templates.TemplateResponse(request=request, name="review.html", context={
        "reports": reports, "review": review, "verification": verification,
        "page": page, "pages": pages, "total": total, "user": _nav_user(user),
        "pending_count": pending_count,
    })


# ── Broker UI ────────────────────────────────────────────────────────────────────

# Whitelisted sortable columns (header key -> column). Default order is newest-first.
_SORT_COLS = {
    "seen": Truck.detected_at,
    "company": Truck.company_name,
    "vehicle": Truck.license_plate,
    "type": Truck.vehicle_type,
    "source": Truck.source,
}


@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: Optional[str] = None, source: Optional[str] = None,
          vtype: Optional[str] = None, loc: Optional[str] = None,
          verified: str = "all", sort: str = "seen", dir: str = "desc",
          fresh: str = "all", page: int = Query(1, ge=1)):
    from urllib.parse import urlencode
    sort = sort if sort in _SORT_COLS else "seen"
    descending = dir != "asc"
    col = _SORT_COLS[sort]
    primary = col.desc().nulls_last() if descending else col.asc().nulls_last()
    order = (primary,) if sort == "seen" else (primary, Truck.detected_at.desc())
    cutoff = _fresh_cutoff(fresh)  # None for "all" (default — most data is historical)
    fresh = fresh if fresh in _FRESH_WINDOWS else "all"
    verified = "verified" if verified == "verified" else "all"

    def _filtered(stmt):
        stmt = _apply_search(stmt, q, source)
        if cutoff is not None:
            stmt = stmt.where(Truck.detected_at >= cutoff)
        if vtype:
            stmt = stmt.where(Truck.vehicle_type == vtype)
        if loc:
            stmt = stmt.where(or_(Truck.city.ilike(loc), Truck.location.ilike(loc)))
        if verified == "verified":
            stmt = stmt.where(or_(Truck.verification_status == "VERIFIED",
                                  Truck.review_status == "PASSED"))
        return stmt

    user = get_current_user(request)  # drives the shared nav (tabs + login/logout)
    can_review = _can_review(user)
    Session = get_session_factory()
    with Session() as s:
        total = s.execute(_filtered(select(func.count()).select_from(Truck))).scalar_one()
        rows = s.execute(_filtered(select(Truck)).order_by(*order)
                         .limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)).scalars().all()
        types = [t for (t,) in s.execute(
            select(distinct(Truck.vehicle_type)).where(Truck.vehicle_type.isnot(None))
            .order_by(Truck.vehicle_type)).all()]
        cities = [c for (c,) in s.execute(
            select(distinct(Truck.city)).where(Truck.city.isnot(None))
            .order_by(Truck.city)).all()]
        pending_count = _pending_reports(s) if can_review else 0

    trucks = []
    for r in rows:
        d = r.as_dict()
        d["time_ago"] = _time_ago(r.detected_at)
        d["detected_at_human"] = r.detected_at.strftime("%Y-%m-%d %H:%M") if r.detected_at else ""
        d["fresh"] = _fresh_bucket(r.detected_at)
        trucks.append(d)

    show_load = any(t["loaded_status"] for t in trucks)
    show_location = any((t.get("location") or t.get("city")) for t in trucks)
    active_filters = sum(bool(x) for x in (q, source, vtype, loc)) \
        + (fresh != "all") + (verified != "all")
    # Filters only (no sort/page) — used to build sort/pagination links cleanly.
    base_qs = urlencode({"q": q or "", "source": source or "", "vtype": vtype or "",
                         "loc": loc or "", "fresh": fresh, "verified": verified})

    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    return templates.TemplateResponse(request=request, name="index.html", context={
        "trucks": trucks, "q": q or "", "source": source or "",
        "vtype": vtype or "", "loc": loc or "", "verified": verified,
        "types": types, "cities": cities, "active_filters": active_filters,
        "base_qs": base_qs,
        "sort": sort, "dir": "asc" if not descending else "desc", "fresh": fresh,
        "show_load": show_load, "show_location": show_location,
        "page": page, "pages": pages, "total": total,
        "user": _nav_user(user), "pending_count": pending_count,
    })
