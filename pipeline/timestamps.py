"""Derive an absolute wall-clock time for a sighting.

Video frames carry only a *relative* offset (seconds from the start of the file).
Brokers need real recency to sort "newest first", so we recover the recording
datetime from the DVR filename — e.g. ``D01_20230331124308.mp4`` encodes
2023-03-31 12:43:08 — and add the frame offset to it.
"""
import re
from datetime import datetime, timedelta
from typing import Optional

# A run of exactly 14 digits = YYYYMMDDHHMMSS, the DVR timestamp.
_TS14_RE = re.compile(r'(?<!\d)(\d{14})(?!\d)')


def parse_recording_dt(filename: str) -> Optional[datetime]:
    """Parse the recording start time from a DVR filename.

    'D01_20230331124308.mp4' -> datetime(2023, 3, 31, 12, 43, 8).
    Returns None if no 14-digit timestamp is present or it isn't a valid date.
    """
    if not filename:
        return None
    m = _TS14_RE.search(filename)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def detected_at(source_ref: str, offset_sec: float = 0.0,
                base_dt: Optional[datetime] = None) -> datetime:
    """Absolute timestamp of a sighting.

    - base_dt given            -> base_dt + offset_sec
    - else parse from source_ref (a video filename) -> recording_dt + offset_sec
    - else (live stream / image upload) -> now()
    """
    if base_dt is None:
        base_dt = parse_recording_dt(source_ref or "")
    if base_dt is None:
        return datetime.now()
    return base_dt + timedelta(seconds=offset_sec or 0.0)
