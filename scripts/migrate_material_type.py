"""Add the material_type column to an existing `trucks` table.

New mobile /report field: what cargo the truck carries/is suited for (e.g.
"Steel", "Grains", "Cement"). Same convention as body_type — stored verbatim,
no server-side enum, the app owns the choice list. Lets a telecaller factor
cargo fit into their pass/reject decision in the Review Queue.

`Base.metadata.create_all` only creates missing tables, never new columns on an
existing one, so this idempotent migration ALTERs the table. Safe to run repeatedly.

Usage:
    .venv\\Scripts\\python.exe scripts\\migrate_material_type.py
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
            "ALTER TABLE trucks ADD COLUMN IF NOT EXISTS material_type VARCHAR(64)"))
        print("  ensured column: material_type VARCHAR(64)")
    print("OK — trucks table has the material_type column.")


if __name__ == "__main__":
    main()
