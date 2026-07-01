"""Add the body_type column to an existing `trucks` table.

New mobile /report field: the Android app lets the contributor pick one of a fixed
set of body types (e.g. "Container") and sends it as a plain string. Stored verbatim
- no server-side enum, since the app owns the choice list.

`Base.metadata.create_all` only creates missing tables, never new columns on an
existing one, so this idempotent migration ALTERs the table. Safe to run repeatedly.

Usage:
    .venv\\Scripts\\python.exe scripts\\migrate_body_type.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402
from pipeline.db import get_engine, init_db, database_url  # noqa: E402


def main():
    print(f"Connecting to: {database_url()}")
    init_db()
    with get_engine().begin() as c:
        c.execute(text(
            "ALTER TABLE trucks ADD COLUMN IF NOT EXISTS body_type VARCHAR(32)"))
        print("  ensured column: body_type VARCHAR(32)")
    print("OK — trucks table has the body_type column.")


if __name__ == "__main__":
    main()
