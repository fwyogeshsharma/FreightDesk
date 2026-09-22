"""Add the edit_history column to an existing `trucks` table.

Records telecaller edits made to a PENDING mobile report from the /review page (who,
when, and each changed field's old -> new value), so a correction never erases what
the contributor originally submitted. See the edit_history comment in pipeline/db.py.

`Base.metadata.create_all` only creates missing tables, never new columns on an
existing one, so this idempotent migration ALTERs the table. Safe to run repeatedly.

Usage:
    .venv\\Scripts\\python.exe scripts\\migrate_report_edit_history.py
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
            "ALTER TABLE trucks ADD COLUMN IF NOT EXISTS edit_history JSONB"))
        print("  ensured column: edit_history JSONB")
    print("OK — trucks table has the edit_history column.")


if __name__ == "__main__":
    main()
