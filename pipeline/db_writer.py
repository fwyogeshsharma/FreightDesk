"""Database sink: writes one `trucks` row per closed TruckEvent.

Implements the same duck-typed `.write(event, progress) -> bool` interface as
`TruckCSVWriter`, so `process_video` feeds it with zero changes. Postgres handles
concurrent writers, so in parallel mode each worker owns its own `TruckDBWriter`
and inserts directly — no parent queue needed.
"""
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from .db import SourceType, SubmissionLog, Truck, get_session_factory
from .extract import extract_truck_fields
from .timestamps import detected_at as _detected_at

log = logging.getLogger(__name__)

# Columns a mobile field report may set (image_path stays NULL — images aren't stored).
_REPORT_COLUMNS = (
    "detected_at", "source_ref", "license_plate", "plate_confidence",
    "company_name", "vehicle_type", "city", "other_text", "website",
    "phone_number", "phone_reported", "phone_ocr",
    "loaded_status", "location", "latitude", "longitude", "num_wheels",
    "reported_by", "reported_by_user_id", "reporter_phone",
    "verification_status", "frames", "plate_candidates", "body_texts",
)


def insert_report(fields: dict, images_count: int = None, session_factory=None) -> dict:
    """Insert one mobile field-report row (source=image_api) AND its audit-log entry
    in one transaction; return the truck row as_dict().
    `fields` is the reconciled dict from pipeline.reports.reconcile()."""
    Session = session_factory or get_session_factory()
    row = {k: fields.get(k) for k in _REPORT_COLUMNS}
    # Every new report starts awaiting telecaller review.
    truck = Truck(source=SourceType.image_api, review_status="PENDING", **row)
    s = Session()
    try:
        s.add(truck)
        s.flush()  # assigns truck.id for the audit link
        s.add(SubmissionLog(
            reported_by=fields.get("reported_by"),
            reported_by_user_id=fields.get("reported_by_user_id"),
            reporter_phone=fields.get("reporter_phone"),
            status=fields.get("verification_status"),
            reason=fields.get("reason"),
            vehicle_reported=fields.get("vehicle_reported"),
            vehicle_ocr=fields.get("vehicle_ocr"),
            phone_reported=fields.get("phone_reported"),
            phone_ocr=fields.get("phone_ocr"),
            images_count=images_count,
            truck_id=truck.id,
        ))
        s.commit()
        return truck.as_dict()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


class TruckDBWriter:
    def __init__(self, source: SourceType = SourceType.video,
                 source_ref: Optional[str] = None,
                 base_dt: Optional[datetime] = None,
                 crops_dir: Optional[str] = None,
                 require_phone: bool = False,
                 session_factory=None):
        """
        source        — where these sightings come from (video/image_api/stream).
        source_ref    — overrides event.source_video for image/stream batches.
        base_dt       — explicit recording start; if None it's parsed per-row from
                        the video filename (video source) or falls back to now().
        crops_dir     — directory to save the representative truck photo into.
        require_phone — drop sightings with no readable phone number. Used for
                        video/stream: a telecaller can't act on a truck with no
                        number to call.
        """
        self.source = source
        self.source_ref = source_ref
        self.base_dt = base_dt
        self.require_phone = require_phone
        self._sessionmaker = session_factory or get_session_factory()
        self._crops_dir = Path(crops_dir) if crops_dir else None
        if self._crops_dir:
            self._crops_dir.mkdir(parents=True, exist_ok=True)
        self._count = 0
        self.last_dict = None  # as_dict() of the most recently inserted row

    def write(self, event, progress: dict = None) -> bool:
        """Insert one row for this truck event. Returns False for noise events
        (and, when require_phone is set, for sightings with no phone number)."""
        fields = extract_truck_fields(event)
        if fields is None:
            return False

        if self.require_phone and not fields["phone_number"]:
            return False  # video/stream sighting with no callable number — skip

        ref = self.source_ref or fields.get("source_video") or ""
        if self.source == SourceType.video:
            dt = _detected_at(ref, offset_sec=fields["first_seen_sec"] or 0.0,
                              base_dt=self.base_dt)
        else:
            # Image uploads / live stream: the sighting happens "now".
            dt = self.base_dt or datetime.now()

        truck = Truck(
            detected_at=dt,
            source=self.source,
            source_ref=ref or None,
            license_plate=fields["license_plate"] or None,
            plate_confidence=fields["plate_confidence"],
            company_name=fields["company_name"] or None,
            phone_number=fields["phone_number"] or None,
            website=fields["website"] or None,
            vehicle_type=fields["vehicle_type"] or None,
            city=fields["city"] or None,
            other_text=fields["other_text"] or None,
            frames=fields["frames"],
            first_seen_sec=fields["first_seen_sec"],
            last_seen_sec=fields["last_seen_sec"],
            plate_candidates=fields["plate_candidates"] or None,
            body_texts=fields["body_texts"] or None,
        )

        session = self._sessionmaker()
        try:
            session.add(truck)
            session.flush()            # assigns truck.id
            img_rel = self._save_crop(truck.id, event)
            if img_rel:
                truck.image_path = img_rel
            session.commit()
            self.last_dict = truck.as_dict()
            self._count += 1
            return True
        except Exception:
            session.rollback()
            log.exception("DB write failed for event %s", getattr(event, "event_id", "?"))
            return False
        finally:
            session.close()

    def close(self):
        pass  # sessions are opened/closed per write; nothing to tear down

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _save_crop(self, truck_id: int, event) -> Optional[str]:
        """Persist the representative truck photo; return its path relative to
        the output dir (e.g. 'crops/42.jpg'), or None."""
        crop = getattr(event, "best_crop", None)
        if crop is None or self._crops_dir is None or getattr(crop, "size", 0) == 0:
            return None
        try:
            import cv2
            fname = f"{truck_id}.jpg"
            cv2.imwrite(str(self._crops_dir / fname), crop)
            return f"crops/{fname}"
        except Exception:
            log.exception("Failed to save crop for truck %s", truck_id)
            return None
