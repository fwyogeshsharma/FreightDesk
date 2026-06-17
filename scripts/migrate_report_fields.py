"""Add the mobile field-report columns to an existing `trucks` table.

`Base.metadata.create_all` only creates missing tables, never new columns on an
existing one, so this idempotent migration ALTERs the table. Safe to run repeatedly.

Usage:
    .venv\\Scripts\\python.exe scripts\\migrate_report_fields.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402
from pipeline.db import get_engine, init_db, database_url  # noqa: E402

# column name -> SQL type
_NEW_COLUMNS = {
    "loaded_status": "VARCHAR(16)",
    "location": "VARCHAR(255)",
    "latitude": "DOUBLE PRECISION",
    "longitude": "DOUBLE PRECISION",
    "num_wheels": "INTEGER",
    "phone_reported": "VARCHAR(64)",
    "phone_ocr": "VARCHAR(128)",
    "reported_by": "VARCHAR(128)",
    "verification_status": "VARCHAR(16)",
    "review_status": "VARCHAR(16)",
    "reviewed_by": "VARCHAR(128)",
    "reviewed_at": "TIMESTAMP WITH TIME ZONE",
    "review_note": "VARCHAR(500)",
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
    print("OK — trucks table has the field-report columns.")


if __name__ == "__main__":
    main()
