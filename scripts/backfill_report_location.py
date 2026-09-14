"""One-off backfill: reverse-geocode `location` for mobile reports that have
latitude/longitude but no location text, left over from before reverse-geocoding
existed (added 2026-07-13, see pipeline/geocode.py). Safe to re-run — only
touches rows still missing location.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from pipeline.db import Truck, get_session_factory  # noqa: E402
from pipeline.geocode import reverse_geocode  # noqa: E402

# Nominatim's usage policy caps this at ~1 request/sec.
_DELAY_SEC = 1.1


def main():
    Session = get_session_factory()
    with Session() as session:
        rows = session.scalars(
            select(Truck).where(
                Truck.latitude.is_not(None),
                Truck.longitude.is_not(None),
                (Truck.location.is_(None)) | (Truck.location == ""),
            )
        ).all()
        print(f"Found {len(rows)} rows to backfill")

        filled = failed = 0
        for i, row in enumerate(rows, 1):
            name = reverse_geocode(row.latitude, row.longitude)
            if name:
                row.location = name
                filled += 1
            else:
                failed += 1
            if i % 10 == 0 or i == len(rows):
                session.commit()
                print(f"  {i}/{len(rows)} processed ({filled} filled, {failed} failed)")
            time.sleep(_DELAY_SEC)

        session.commit()
        print(f"Done. Filled {filled}, failed {failed} of {len(rows)}.")


if __name__ == "__main__":
    main()
