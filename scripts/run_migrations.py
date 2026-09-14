"""Apply pending scripts/migrate_*.py, skipping ones already applied.

Every migration here is individually idempotent (ALTER TABLE ... ADD COLUMN IF NOT
EXISTS and friends), so re-running them all was always *safe* — just slow. deploy.sh
used to spend most of its time starting one container per script to re-apply work that
was already done. This runner does two things about that:

  1. One process for the whole set, instead of one container per script.
  2. A `schema_migrations` ledger, so an applied migration is skipped entirely on the
     next deploy. Steady state is "0 pending", which costs a single query.

**Auto-discovery is preserved deliberately.** The scripts are still found by globbing
scripts/migrate_*.py rather than read from a hardcoded list — a newly added migration
is never silently skipped just because someone forgot to register it (that caused a
prod outage once, see deploy.sh). The ledger only ever suppresses a file that has
already run to completion here.

A migration is recorded ONLY after its process exits 0. A failure stops the run
immediately, leaves the ledger unchanged, and returns non-zero so deploy.sh's `set -e`
aborts the deploy — so a half-applied schema is never marked as done.

Each script runs as its own subprocess, exactly as deploy.sh used to invoke it. That
keeps the existing contract (standalone script, own engine, own transaction) and means
one migration's imports or SQLAlchemy state cannot leak into the next.

Usage:
    python scripts/run_migrations.py            # apply pending
    python scripts/run_migrations.py --list     # show status, change nothing
    python scripts/run_migrations.py --all      # re-apply everything, ignore ledger
"""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402
from pipeline.db import get_engine, database_url  # noqa: E402

_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    name        VARCHAR(255) PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def _ensure_ledger(conn):
    conn.execute(text(_LEDGER_DDL))


def _applied(conn) -> set:
    return {r[0] for r in conn.execute(text("SELECT name FROM schema_migrations"))}


def _record(conn, name: str):
    conn.execute(
        text("INSERT INTO schema_migrations (name) VALUES (:n) "
             "ON CONFLICT (name) DO NOTHING"),
        {"n": name},
    )


def _discover():
    """Every migration script, in filename order — never a hardcoded list."""
    return sorted(p for p in (ROOT / "scripts").glob("migrate_*.py"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true",
                    help="show which migrations are applied/pending; applies none "
                         "(it does create the ledger table if missing)")
    ap.add_argument("--all", action="store_true",
                    help="re-apply every migration, ignoring the ledger")
    args = ap.parse_args()

    print(f"Connecting to: {database_url()}")
    engine = get_engine()
    with engine.begin() as c:
        _ensure_ledger(c)
        applied = _applied(c)

    scripts = _discover()
    if not scripts:
        print("No scripts/migrate_*.py found — nothing to do.")
        return 0

    if args.list:
        print(f"\n{len(scripts)} migration(s):")
        for p in scripts:
            status = "applied" if p.name in applied else "PENDING"
            print(f"  [{status}] {p.name}")
        pending = [p for p in scripts if p.name not in applied]
        print(f"\n{len(pending)} pending.")
        return 0

    pending = scripts if args.all else [p for p in scripts if p.name not in applied]
    skipped = len(scripts) - len(pending)

    if not pending:
        print(f"All {len(scripts)} migration(s) already applied — nothing to do.")
        return 0

    print(f"{len(pending)} pending, {skipped} already applied.\n")
    for p in pending:
        print(f"  -> {p.name}")
        proc = subprocess.run([sys.executable, str(p)], cwd=str(ROOT))
        if proc.returncode != 0:
            print(f"\nFAILED: {p.name} exited {proc.returncode}. "
                  f"Not recorded as applied; later migrations were not run.")
            return proc.returncode
        with engine.begin() as c:
            _record(c, p.name)

    print(f"\nOK — applied {len(pending)} migration(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
