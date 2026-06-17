"""Create the PostgreSQL `trucks` table and indexes.

Usage:
    .venv\\Scripts\\python.exe scripts\\init_db.py

Reads the connection string from $DATABASE_URL (default:
postgresql+psycopg://postgres:postgres@localhost:5432/trucks).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.db import init_db, database_url  # noqa: E402


def main():
    url = database_url()
    print(f"Connecting to: {url}")
    try:
        init_db()
    except Exception as e:
        print(f"\nFAILED: {e}\n")
        print("Check that PostgreSQL is running and the 'trucks' database exists.")
        print("Create it with:  createdb -U postgres trucks")
        sys.exit(1)
    print("OK — 'trucks' table and indexes are ready.")


if __name__ == "__main__":
    main()
