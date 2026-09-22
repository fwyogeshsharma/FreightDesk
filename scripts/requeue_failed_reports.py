"""Put FAILED mobile reports back on the OCR queue.

`processing_status=FAILED` is terminal: `webapp/processing.py::recover_pending()` only
re-enqueues rows left QUEUED/PROCESSING by a crash, so a report that failed for a
transient reason (the YOLO weights download 504ing, an OOM, a bad deploy) stays failed
forever even once the cause is fixed. This flips those rows back to QUEUED.

Reprocessing is only possible at all because report photos are now retained
indefinitely — under the old ~2-day rule a failure older than that was unrecoverable.

Rows whose photos are no longer in storage are skipped: requeuing them would just burn
a worker slot and fail again. That is the common case for anything predating the
retention change, so expect skips on old rows.

**Two steps — the script alone is not enough.** It runs in its own container
(`docker-compose run` starts a new one), so it cannot reach the in-memory queue inside
the *running* web container. It updates the DB; the web container must then be
restarted, at which point recover_pending() picks up everything marked QUEUED:

    sudo docker-compose run --rm web python scripts/requeue_failed_reports.py
    sudo docker-compose run --rm web python scripts/requeue_failed_reports.py --apply
    sudo docker-compose restart web   # <- required, or nothing is processed

(On the VM. Locally: `.venv\\Scripts\\python.exe scripts\\requeue_failed_reports.py`, then
restart run_webapp.bat.)

Dry run by default. Options:
    --ids 8514 8515 ...   only these report ids
    --min-id N            only ids >= N (e.g. the start of one bad window)
    --include-missing     requeue even if the photos are gone (they will fail again)
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from pipeline.db import Truck, get_session_factory, database_url  # noqa: E402
from pipeline.storage import get_storage  # noqa: E402


def _photos_present(storage, keys) -> bool:
    """True if at least one of the report's photos is still readable."""
    for key in (keys or []):
        try:
            if storage.get(key) is not None:
                return True
        except Exception:
            continue
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="actually requeue (default: dry run)")
    ap.add_argument("--ids", type=int, nargs="+", help="only these report ids")
    ap.add_argument("--min-id", type=int, help="only ids >= this")
    ap.add_argument("--include-missing", action="store_true",
                    help="requeue even when the photos are gone (they will fail again)")
    args = ap.parse_args()

    print(f"database: {database_url()}")
    print(f"mode    : {'APPLY' if args.apply else 'DRY RUN (nothing will change)'}\n")

    storage = get_storage()
    Session = get_session_factory()
    requeued = skipped_missing = 0

    with Session() as s:
        stmt = select(Truck).where(Truck.processing_status == "FAILED")
        if args.ids:
            stmt = stmt.where(Truck.id.in_(args.ids))
        if args.min_id is not None:
            stmt = stmt.where(Truck.id >= args.min_id)
        rows = s.execute(stmt.order_by(Truck.id)).scalars().all()

        if not rows:
            print("No FAILED reports match — nothing to do.")
            return 0
        print(f"{len(rows)} FAILED report(s) match.\n")

        for row in rows:
            keys = row.image_keys or []
            have = bool(keys) and _photos_present(storage, keys)
            if not have and not args.include_missing:
                print(f"  skip  {row.id}  photos no longer in storage")
                skipped_missing += 1
                continue
            err = (row.processing_error or "").strip().replace("\n", " ")[:60]
            print(f"  queue {row.id}  {len(keys)} photo(s)  was: {err or '-'}")
            if args.apply:
                row.processing_status = "QUEUED"
                row.processing_error = None
            requeued += 1

        if args.apply:
            s.commit()

    print(f"\n{'requeued' if args.apply else 'would requeue'}: {requeued} report(s)")
    if skipped_missing:
        print(f"skipped (photos gone): {skipped_missing} report(s)")
    if args.apply and requeued:
        print("\nNOW RESTART THE WEB CONTAINER - this script updated the database, but "
              "the running\nworker's queue lives in another process and has not been "
              "told about these rows:\n    sudo docker-compose restart web")
    elif not args.apply:
        print("\nDry run only. Re-run with --apply, then restart the web container.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
