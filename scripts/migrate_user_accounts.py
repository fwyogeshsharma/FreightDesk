"""Add the user-accounts layer to an existing database.

`Base.metadata.create_all` creates the new `users` + `user_sessions` tables but never
adds columns to an existing table, so this idempotent migration also ALTERs `trucks` and
`submission_log` to add the reporter/reviewer attribution columns, then seeds an admin.
Safe to run repeatedly.

Usage:
    .venv\\Scripts\\python.exe scripts\\migrate_user_accounts.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402
from pipeline.db import (  # noqa: E402
    database_url, get_engine, get_session_factory, init_db,
)
from pipeline.auth import ensure_seed_admin  # noqa: E402

# table -> {column name: SQL type}
_NEW_COLUMNS = {
    "trucks": {
        "reported_by_user_id": "BIGINT",
        "reporter_phone": "VARCHAR(32)",
        "reviewed_by_user_id": "BIGINT",
    },
    "submission_log": {
        "reported_by_user_id": "BIGINT",
        "reporter_phone": "VARCHAR(32)",
    },
}


def main():
    print(f"Connecting to: {database_url()}")
    # Creates the new users + user_sessions tables (and any other missing tables).
    init_db()
    with get_engine().begin() as c:
        for table, cols in _NEW_COLUMNS.items():
            for name, sqltype in cols.items():
                c.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {sqltype}"))
                print(f"  ensured column: {table}.{name} {sqltype}")

    # Seed an admin so the deploy is usable (no-op if one already exists).
    Session = get_session_factory()
    with Session() as s:
        admin = ensure_seed_admin(s)
        seeded_phone = admin.phone if admin else None
        s.commit()
    if seeded_phone:
        print(f"  seeded admin: phone={seeded_phone} "
              f"(login with ADMIN_PASSWORD; set ADMIN_PHONE to customize)")
    else:
        print("  admin already present — left unchanged")
    print("OK — user accounts ready and attribution columns ensured.")


if __name__ == "__main__":
    main()
