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

        # Upgrade the users table to the hybrid login model: contributors keep logging
        # in by phone; operators (telecaller/admin) log in by a new unique username, and
        # phone becomes optional for them.
        c.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS username VARCHAR(64)"))
        c.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_username ON users (username)"))
        c.execute(text("ALTER TABLE users ALTER COLUMN phone DROP NOT NULL"))
        # An earlier deploy may have seeded the admin with a non-numeric phone login id
        # (e.g. 'admin'); move that into the username column so it logs in by username.
        promoted = c.execute(text(
            "UPDATE users SET username = lower(phone), phone = NULL "
            "WHERE username IS NULL AND phone IS NOT NULL AND phone !~ '^\\+?[0-9]+$'"))
        print(f"  users: username column + index ensured; phone now optional; "
              f"promoted {promoted.rowcount} legacy login id(s) to username")

    # Seed an admin so the deploy is usable (no-op if one already exists).
    Session = get_session_factory()
    with Session() as s:
        admin = ensure_seed_admin(s)
        seeded_username = admin.username if admin else None
        s.commit()
    if seeded_username:
        print(f"  seeded admin: username={seeded_username} "
              f"(login with ADMIN_PASSWORD; set ADMIN_USERNAME to customize)")
    else:
        print("  admin already present — left unchanged")
    print("OK — user accounts ready and attribution columns ensured.")


if __name__ == "__main__":
    main()
