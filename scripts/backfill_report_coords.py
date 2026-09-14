"""One-off backfill: forward-geocode latitude/longitude for reports that have
location text but no coordinates (pipeline/geocode.py::forward_geocode, added
2026-07-16) — mostly legacy rows from the payment-report import, whose source
spreadsheet never had GPS coordinates. The Android app's map view only ever
reads latitude/longitude back, never the location text, so these rows are
invisible on the map without this. Safe to re-run — only touches rows still
missing coordinates.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from pipeline.db import Truck, get_session_factory  # noqa: E402
from pipeline.geocode import forward_geocode  # noqa: E402

# Nominatim's usage policy caps this at ~1 request/sec.
_DELAY_SEC = 1.1
_BATCH = 50


def main():
    Session = get_session_factory()
    with Session() as session:
        rows = session.scalars(
            select(Truck).where(
                Truck.location.is_not(None),
                Truck.location != "",
                (Truck.latitude.is_(None)) | (Truck.longitude.is_(None)),
            )
        ).all()
        print(f"Found {len(rows)} rows to backfill", flush=True)

        filled = failed = 0
        for i, row in enumerate(rows, 1):
            coords = forward_geocode(row.location)
            if coords:
                row.latitude, row.longitude = coords
                filled += 1
            else:
                failed += 1
            if i % _BATCH == 0 or i == len(rows):
                session.commit()
                print(f"  {i}/{len(rows)} processed ({filled} filled, {failed} failed)", flush=True)
            time.sleep(_DELAY_SEC)

        session.commit()
        print(f"Done. Filled {filled}, failed {failed} of {len(rows)}.", flush=True)


if __name__ == "__main__":
    main()
