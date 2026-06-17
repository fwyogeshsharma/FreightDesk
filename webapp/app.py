"""Broker-facing web app + JSON API + still-image ingestion.

Run:  uvicorn webapp.app:app --host 0.0.0.0 --port 8000
(or use run_webapp.bat)

- GET  /                      newest-first truck cards, searchable, click-to-call
- GET  /api/trucks            JSON list (q, source, limit, offset)
- GET  /api/trucks/{id}       JSON detail
- POST /api/trucks/report     mobile field report: 1-5 photos + fields -> one record
- PATCH /api/trucks/{id}      telecaller review decision (admin auth)
- GET  /review                telecaller review queue (admin auth)

Photos are never stored on the system — every source processes frames/images and
keeps only the extracted data.
"""
import hmac
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from fastapi import (
    Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import distinct, func, or_, select

from pipeline.config import Config
from pipeline.db import SourceType, Truck, get_session_factory, init_db

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
    # Create the table if it isn't there yet; browsing an empty DB still works.
    try:
        init_db()
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


# ── Admin auth (simple shared password) ──────────────────────────────────────────
# Protects the telecaller review page and the PATCH endpoint, which together decide
# reward eligibility. Upgrade to per-user accounts later; keep behind HTTPS.

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
if not ADMIN_PASSWORD:
    ADMIN_PASSWORD = "admin"
    print("[webapp] WARNING: ADMIN_PASSWORD not set — using default 'admin'. "
          "Set ADMIN_PASSWORD before deploying.")


def _token_ok(value: str) -> bool:
    return bool(value) and hmac.compare_digest(value, ADMIN_PASSWORD)


def _check_admin(request: Request) -> Optional[str]:
    """Return the admin identity if authed (cookie for the browser, X-Admin-Token
    header for API/automation), else None."""
    tok = request.cookies.get("admin_token") or request.headers.get("X-Admin-Token")
    return ADMIN_USER if _token_ok(tok or "") else None


def require_admin(request: Request) -> str:
    """FastAPI dependency for JSON endpoints — 401 if not authed."""
    who = _check_admin(request)
    if not who:
        raise HTTPException(401, "Admin authentication required")
    return who


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
    images: List[UploadFile] = File(...),
    phone_number: str = Form(...),
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
    })


@app.get("/report-test", response_class=HTMLResponse)
def report_test(request: Request):
    """Browser form to submit a field report — for testing before the mobile app
    is wired (Swagger UI can't do multi-file upload reliably)."""
    return templates.TemplateResponse(request=request, name="report_test.html", context={})


# ── Telecaller review: PATCH decision + queue page (admin auth) ────────────────────

_REVIEW_STATES = {"PENDING", "PASSED", "REJECTED"}


class ReviewPatch(BaseModel):
    review_status: str
    review_note: Optional[str] = None
    reviewed_by: Optional[str] = None


@app.patch("/api/trucks/{truck_id}")
def patch_truck(truck_id: int, body: ReviewPatch, admin: str = Depends(require_admin)):
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
        row.reviewed_by = (body.reviewed_by or "").strip() or admin
        row.reviewed_at = datetime.now()
        row.review_note = (body.review_note or "").strip() or None
        s.commit()
        return JSONResponse(row.as_dict())


@app.get("/review/login", response_class=HTMLResponse)
def review_login_form(request: Request, error: Optional[str] = None):
    return templates.TemplateResponse(request=request, name="login.html",
                                      context={"error": bool(error)})


@app.post("/review/login")
def review_login(password: str = Form(...)):
    if not _token_ok(password):
        return RedirectResponse("/review/login?error=1", status_code=303)
    resp = RedirectResponse("/review", status_code=303)
    # Cookie value is the shared secret; httponly so page JS can't read it.
    resp.set_cookie("admin_token", password, httponly=True, samesite="lax", max_age=86400)
    return resp


@app.get("/review/logout")
def review_logout():
    resp = RedirectResponse("/review/login", status_code=303)
    resp.delete_cookie("admin_token")
    return resp


def _pending_reports(s) -> int:
    """Count field reports awaiting telecaller review (for the nav badge)."""
    return s.execute(select(func.count()).select_from(Truck).where(
        Truck.source == SourceType.image_api,
        Truck.review_status == "PENDING")).scalar_one()


@app.get("/review", response_class=HTMLResponse)
def review(request: Request, review: str = "pending",
           verification: str = "all", page: int = Query(1, ge=1)):
    who = _check_admin(request)
    if not who:
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
        "page": page, "pages": pages, "total": total, "admin": who,
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

    admin = _check_admin(request)  # drives the shared nav (tabs + login/logout)
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
        pending_count = _pending_reports(s) if admin else 0

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
        "admin": admin, "pending_count": pending_count,
    })
