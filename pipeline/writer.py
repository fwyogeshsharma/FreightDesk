"""CSV writer — appends one row per truck event as soon as the event closes.

All text-extraction logic now lives in `pipeline/extract.py` and is shared with
the database writer and the image API. This module is a thin CSV adapter: it adds
the sequential `truck_id` and run-progress columns that are specific to CSV runs.
"""
import csv
import time
from pathlib import Path

from .tracker import TruckEvent
from .extract import extract_truck_fields, fmt_ts as _fmt_ts
# Re-exported for backward compatibility (main.py imports these from here)
from .extract import is_noise_event, deduplicate  # noqa: F401

CSV_FILENAME = "trucks.csv"

FIELDNAMES = [
    "truck_id",         # sequential event id (T0001, T0002, ...)
    "video_file",       # source video filename
    "first_seen",       # HH:MM:SS
    "last_seen",        # HH:MM:SS
    "frames",           # number of frames this truck appeared in
    "license_plate",    # best plate reading
    "plate_confidence", # HIGH / LOW / NONE
    "company_name",     # company or organization name on truck
    "phone_number",     # contact number(s)
    "website",          # website URL(s)
    "vehicle_type",     # SCHOOL BUS / GOODS CARRIER / TANKER / BUS / TRUCK etc.
    "city",             # city/route mentions (Jaipur, Delhi, etc.)
    "other_text",       # remaining clean text that doesn't fit above
    "elapsed_time",     # cumulative wall-clock HH:MM:SS since the run started
    "video_pct",        # how far into the current video processing has reached
    "est_remaining",    # estimated wall-clock time left for the current video
]


class TruckCSVWriter:
    def __init__(self, output_dir: str):
        self._path = Path(output_dir) / CSV_FILENAME
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._counter = 0
        self._t_start = time.monotonic()

        # Each run reprocesses all videos from scratch, so always start a fresh
        # CSV — appending would duplicate rows from interrupted earlier runs.
        self._fh = open(self._path, "w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=FIELDNAMES)
        self._writer.writeheader()
        self._fh.flush()

    def write(self, event: TruckEvent, progress: dict = None) -> bool:
        """Append one row for this truck event. Returns False for noise events
        (no plate, no text, seen only briefly — usually a false detection).
        progress: optional {'video_pct': str, 'est_remaining': str} from the caller."""
        fields = extract_truck_fields(event)
        if fields is None:
            return False

        # truck_id: a plain sequential identifier for the truck event.
        # Identification data (plate, company) lives in its own columns.
        self._counter += 1
        truck_id = f"T{self._counter:04d}"
        elapsed = time.monotonic() - self._t_start

        row = {
            'truck_id':         truck_id,
            'video_file':       fields['source_video'],
            'first_seen':       _fmt_ts(fields['first_seen_sec']),
            'last_seen':        _fmt_ts(fields['last_seen_sec']),
            'frames':           fields['frames'],
            'license_plate':    fields['license_plate'],
            'plate_confidence': fields['plate_confidence'],
            'company_name':     fields['company_name'],
            'phone_number':     fields['phone_number'],
            'website':          fields['website'],
            'vehicle_type':     fields['vehicle_type'],
            'city':             fields['city'],
            'other_text':       fields['other_text'],
            'elapsed_time':     _fmt_ts(elapsed),
            'video_pct':        (progress or {}).get('video_pct', ''),
            'est_remaining':    (progress or {}).get('est_remaining', ''),
        }

        self._writer.writerow(row)
        self._fh.flush()
        return True

    def close(self):
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
