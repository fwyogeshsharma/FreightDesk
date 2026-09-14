"""Re-key stored report photos into the date-first layout.

The upload path has used three key layouts over time:

  A  reports/<id>/<idx>.jpg                 (original)
  B  reports/<id>_<YYYY-MM-DD>/<idx>.jpg    (briefly, 2026-09-14)
  C  reports/<YYYY-MM-DD>/<id>/<idx>.jpg    (current)

C puts every photo from one day under a single prefix, so "delete everything from
that day" is one folder in the console or one `gcloud storage rm -r`. A and B spread
a day across hundreds of per-report folders, which at ~40 reports/hour is unusable.

Nothing *needs* this migration — photos are always fetched by the key recorded in
`trucks.image_keys`, so all three layouts resolve fine. It exists purely so a day's
photos are actually contiguous in the bucket.

Where the date comes from:
  - layout B: the date already in the key (no guessing).
  - layout A: the stored object's own creation time, in UTC, read before the copy.
Both match what the upload path would have written at the time.

**Run this on the VM**, where DATABASE_URL points at the prod DB and the GCS
credentials are mounted — it has to update `trucks.image_keys` in the same pass:

    docker compose run --rm web python scripts/migrate_photo_keys_date_first.py
    docker compose run --rm web python scripts/migrate_photo_keys_date_first.py --apply

Dry-run by default: it prints every move and changes nothing. Add --apply to execute.
--only-suffix limits the run to layout B (today's folders) and leaves A alone.

Safety:
  - Per row, the order is copy -> verify -> update DB -> delete old. If anything
    fails partway, the OLD object is still present and `image_keys` still points at
    it, so a report never loses its photos. A crash between copy and DB update just
    leaves a harmless orphan, which a re-run cleans up.
  - Idempotent: keys already in layout C are skipped, so re-running is safe.
  - Copying resets the object's GCS timeCreated to the migration time. The upload
    date survives in the key itself and in trucks.detected_at/created_at, but if you
    rely on timeCreated for anything, capture it before running this.
"""
import argparse
import re
import sys
from datetime import timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from pipeline.db import Truck, get_session_factory, database_url  # noqa: E402
from pipeline.storage import get_storage, GCSStorage  # noqa: E402

_LAYOUT_A = re.compile(r'^reports/(\d+)/(\d+\.\w+)$')
_LAYOUT_B = re.compile(r'^reports/(\d+)_(\d{4}-\d{2}-\d{2})/(\d+\.\w+)$')
_LAYOUT_C = re.compile(r'^reports/\d{4}-\d{2}-\d{2}/\d+/\d+\.\w+$')

_CTYPE_BY_EXT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".png": "image/png", ".webp": "image/webp"}


def _created_date(storage, key: str):
    """UTC date the object was stored, or None if that can't be determined."""
    if not isinstance(storage, GCSStorage):
        return None
    try:
        blob = storage._blob(key)
        blob.reload()
        if blob.time_created:
            return blob.time_created.astimezone(timezone.utc).strftime("%Y-%m-%d")
    except Exception as e:
        print(f"    ! could not read timeCreated for {key}: {type(e).__name__}: {e}")
    return None


def _target_key(storage, key: str, only_suffix: bool):
    """New date-first key for `key`, or None to leave it alone."""
    if _LAYOUT_C.match(key):
        return None                      # already migrated
    m = _LAYOUT_B.match(key)
    if m:
        truck_id, date, leaf = m.groups()
        return f"reports/{date}/{truck_id}/{leaf}"
    if only_suffix:
        return None
    m = _LAYOUT_A.match(key)
    if m:
        truck_id, leaf = m.groups()
        date = _created_date(storage, key)
        if not date:
            return None                  # no trustworthy date; leave it where it is
        return f"reports/{date}/{truck_id}/{leaf}"
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="actually move objects and update the DB (default: dry run)")
    ap.add_argument("--only-suffix", action="store_true",
                    help="only migrate layout B (reports/<id>_<date>/...), skip layout A")
    args = ap.parse_args()

    print(f"database: {database_url()}")
    storage = get_storage()
    print(f"storage : {type(storage).__name__}")
    print(f"mode    : {'APPLY' if args.apply else 'DRY RUN (nothing will change)'}\n")

    Session = get_session_factory()
    moved = skipped = failed = 0
    rows_changed = 0

    with Session() as s:
        rows = s.execute(
            select(Truck).where(Truck.image_keys.is_not(None)).order_by(Truck.id)
        ).scalars().all()
        print(f"{len(rows)} row(s) with stored photos\n")

        for row in rows:
            keys = list(row.image_keys or [])
            new_keys = list(keys)
            row_moves = []

            for i, key in enumerate(keys):
                target = _target_key(storage, key, args.only_suffix)
                if not target:
                    skipped += 1
                    continue
                row_moves.append((i, key, target))

            if not row_moves:
                continue

            print(f"truck {row.id}:")
            for i, key, target in row_moves:
                print(f"    {key}  ->  {target}")
                if not args.apply:
                    moved += 1
                    new_keys[i] = target
                    continue
                try:
                    data = storage.get(key)
                    if data is None:
                        print("    ! source object missing - leaving key as is")
                        failed += 1
                        continue
                    ext = Path(key).suffix.lower()
                    storage.put(target, data,
                                content_type=_CTYPE_BY_EXT.get(ext, "image/jpeg"))
                    check = storage.get(target)
                    if check is None or len(check) != len(data):
                        print("    ! copy failed verification - old object kept")
                        failed += 1
                        continue
                    new_keys[i] = target
                    moved += 1
                except Exception as e:
                    print(f"    ! {type(e).__name__}: {e} - old object kept")
                    failed += 1

            if new_keys == keys:
                continue

            if args.apply:
                # DB first, delete after: while both objects exist, either key works.
                row.image_keys = new_keys
                s.commit()
                rows_changed += 1
                for i, key, target in row_moves:
                    if new_keys[i] != target:
                        continue                      # this one failed; keep the old
                    try:
                        storage.delete(key)
                    except Exception as e:
                        print(f"    ! could not delete old {key}: {type(e).__name__}: {e}")
            else:
                rows_changed += 1

    print(f"\n{'would move' if not args.apply else 'moved'}: {moved} object(s) "
          f"across {rows_changed} row(s)")
    print(f"left alone: {skipped} object(s) (already date-first, or no date available)")
    if failed:
        print(f"FAILED: {failed} object(s) - their rows still point at the old keys")
    if not args.apply:
        print("\nDry run only. Re-run with --apply to perform the migration.")


if __name__ == "__main__":
    main()
