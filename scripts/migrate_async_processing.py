"""Add the async-processing columns to an existing `trucks` table.

The mobile /report API is asynchronous: it accepts the report, returns immediately,
and OCRs the photos in a background worker. These columns hold the job lifecycle and
the (temporary, ≤2-day) storage keys for the uploaded photos.

`Base.metadata.create_all` only creates missing tables, never new columns on an
existing one, so this idempotent migration ALTERs the table. Safe to run repeatedly.

Usage:
    .venv\\Scripts\\python.exe scripts\\migrate_async_processing.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402
from pipeline.db import get_engine, init_db, database_url  # noqa: E402

# column name -> SQL type
_NEW_COLUMNS = {
    "processing_status": "VARCHAR(16)",          # QUEUED / PROCESSING / DONE / FAILED
    "processing_error": "VARCHAR(500)",
    "processed_at": "TIMESTAMP WITH TIME ZONE",
    "image_keys": "JSONB",                       # storage keys for the uploaded photos
}

_INDEXES = {
    # The worker scans for unfinished jobs on startup — index the status.
    "ix_trucks_processing_status": "processing_status",
}


def main():
    print(f"Connecting to: {database_url()}")
    # Creates any missing tables (incl. submission_log) on fresh or existing DBs.
    init_db()
    with get_engine().begin() as c:
        for name, sqltype in _NEW_COLUMNS.items():
            c.execute(text(
                f"ALTER TABLE trucks ADD COLUMN IF NOT EXISTS {name} {sqltype}"))
            print(f"  ensured column: {name} {sqltype}")
        for idx, col in _INDEXES.items():
            c.execute(text(
                f"CREATE INDEX IF NOT EXISTS {idx} ON trucks ({col})"))
            print(f"  ensured index: {idx} ({col})")
    print("OK — trucks table has the async-processing columns.")


if __name__ == "__main__":
    main()
