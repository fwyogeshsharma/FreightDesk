"""Background OCR worker for mobile field reports.

`POST /report` accepts a report, stores the photos, inserts a QUEUED row, and returns
immediately. This module owns what happens next: a single background thread pulls
truck ids off an in-process queue and runs the detect→OCR→reconcile chain **one at a
time** (so two OCR passes can't exhaust the small VM's RAM), then marks the row
DONE/FAILED for the app to poll.

Durability: the photos live in storage (≤2 days) and `processing_status` is the source
of truth, so on startup we re-enqueue any rows a crash/restart left QUEUED/PROCESSING
and finish them. Rows whose photos have already expired are marked FAILED.

Single-process assumption: the in-memory queue belongs to ONE uvicorn worker. The VM
runs a single worker (see Dockerfile/COMMANDS.md). Do not scale to multiple uvicorn
workers without moving to a shared queue (e.g. a DB-polled or Redis-backed one).
"""
import logging
import queue
import threading
import time

from pipeline.config import Config
from pipeline.db import Truck, get_session_factory
from pipeline import db_writer
from pipeline.extract import extract_truck_fields
from pipeline.image_api import build_event_from_images
from pipeline.reports import reconcile
from pipeline.storage import get_storage

log = logging.getLogger("freightdesk.worker")

# Make worker INFO logs visible in the server output (uvicorn doesn't configure our
# logger), so report progress shows up in `docker compose logs -f web` on the VM.
_fd_logger = logging.getLogger("freightdesk")
if not _fd_logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    _fd_logger.addHandler(_h)
    _fd_logger.setLevel(logging.INFO)
    _fd_logger.propagate = False

_SWEEP_INTERVAL_SEC = 6 * 3600  # local sweeper cadence (gcs backend: purge is a no-op)


class _Models:
    """Lazily-built ML bundle (YOLO vehicle + plate detectors + EasyOCR). Held
    resident once loaded; the single worker thread is the only caller, so no lock."""

    def __init__(self):
        self.ready = False

    def load(self):
        if self.ready:
            return
        from pipeline.detector import Detector, PlateDetector
        from pipeline.ocr_engine import build_ocr_engine, FrameOCR
        cfg = Config()
        cfg.blur_variance_threshold = 0.0  # phone photos can be a touch soft
        self.detector = Detector(cfg)
        pd = PlateDetector()
        self.plate_detector = pd if pd.available() else None
        self.frame_ocr = FrameOCR(cfg, build_ocr_engine(cfg))
        self.ready = True


models = _Models()

_q: "queue.Queue[int]" = queue.Queue()
_started = False
_lock = threading.Lock()


def enqueue(truck_id: int) -> None:
    _q.put(truck_id)


def _decode_images(image_keys):
    import cv2  # lazy: keeps app import light when only the API surface is exercised
    import numpy as np
    storage = get_storage()
    out = []
    for key in (image_keys or []):
        data = storage.get(key)
        if not data:
            continue
        arr = np.frombuffer(data, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is not None:
            out.append(img)
    return out


def _set_processing(truck_id: int) -> None:
    Session = get_session_factory()
    s = Session()
    try:
        row = s.get(Truck, truck_id)
        if row is not None:
            row.processing_status = "PROCESSING"
            s.commit()
    finally:
        s.close()


def process_truck(truck_id: int) -> None:
    """Run the OCR pipeline for one queued report and finalize/fail its row."""
    Session = get_session_factory()
    s = Session()
    try:
        row = s.get(Truck, truck_id)
        if row is None:
            return
        image_keys = list(row.image_keys) if row.image_keys else []
        reported = db_writer.reported_from_row(row)
    finally:
        s.close()

    try:
        _set_processing(truck_id)
        images = _decode_images(image_keys)
        if not images:
            db_writer.fail_report(
                truck_id, "uploaded photos are no longer available (expired or unreadable)")
            log.warning("report %s FAILED: no usable images", truck_id)
            return
        models.load()
        event = build_event_from_images(
            images, models.detector, models.plate_detector, models.frame_ocr,
            capture_crop=False)
        # image_api photos carry no OSD clock overlay (unlike video/stream frames),
        # so a short digit-only OCR read is far more likely a partial plate/series
        # fragment than clock noise — trust it instead of discarding it.
        ocr = extract_truck_fields(event, allow_digit_fragments=True) or {}
        fields = reconcile(reported, ocr, Config())
        db_writer.finalize_report(truck_id, fields, images_count=len(images))
        log.info("report %s DONE (%s)", truck_id, fields.get("verification_status"))
    except Exception as e:  # worker must never die on one bad job
        log.exception("report %s processing failed", truck_id)
        try:
            db_writer.fail_report(truck_id, f"processing error: {e}")
        except Exception:
            log.exception("could not mark report %s FAILED", truck_id)


def _worker_loop() -> None:
    log.info("OCR worker started")
    while True:
        truck_id = _q.get()
        try:
            process_truck(truck_id)
        finally:
            _q.task_done()


def recover_pending() -> None:
    """Re-enqueue rows left unfinished by a previous run (crash/restart). The photos
    are in storage, so we can finish them; the worker FAILs any whose photos expired."""
    from sqlalchemy import select
    Session = get_session_factory()
    s = Session()
    try:
        ids = s.execute(
            select(Truck.id)
            .where(Truck.processing_status.in_(("QUEUED", "PROCESSING")))
            .order_by(Truck.id)).scalars().all()
    finally:
        s.close()
    for tid in ids:
        enqueue(tid)
    if ids:
        log.info("re-enqueued %d unfinished report(s)", len(ids))


def _sweeper_loop() -> None:
    while True:
        try:
            n = get_storage().purge_expired()
            if n:
                log.info("sweeper deleted %d expired image file(s)", n)
        except Exception:
            log.exception("image sweeper failed")
        time.sleep(_SWEEP_INTERVAL_SEC)


def start_worker() -> None:
    """Start the worker + sweeper threads and recover unfinished jobs. Idempotent."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_worker_loop, name="ocr-worker", daemon=True).start()
    threading.Thread(target=_sweeper_loop, name="image-sweeper", daemon=True).start()
    recover_pending()
