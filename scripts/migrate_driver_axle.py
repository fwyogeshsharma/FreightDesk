"""Add the driver_name and axle_type columns to an existing `trucks` table.

New mobile /report fields: driver_name (who to ask for when the broker calls) and
axle_type (axle configuration, e.g. "2 Axle" / "3 Axle" / "Multi-Axle" — same
free-text convention as body_type). Same idempotent-ALTER pattern as
migrate_material_type.py — safe to run repeatedly.

Usage:
    .venv\\Scripts\\python.exe scripts\\migrate_driver_axle.py
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
            "ALTER TABLE trucks ADD COLUMN IF NOT EXISTS driver_name VARCHAR(128)"))
        print("  ensured column: driver_name VARCHAR(128)")
        c.execute(text(
            "ALTER TABLE trucks ADD COLUMN IF NOT EXISTS axle_type VARCHAR(32)"))
        print("  ensured column: axle_type VARCHAR(32)")
    print("OK — trucks table has the driver_name and axle_type columns.")


if __name__ == "__main__":
    main()
