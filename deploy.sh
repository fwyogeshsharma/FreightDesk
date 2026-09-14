#!/bin/bash
# One-command prod deploy: pull latest code, rebuild the web image, apply every
# migration script (idempotent -- safe to re-run even if a given pull didn't
# touch the schema), then restart. Run this on the VM after every git pull
# instead of remembering the individual docker-compose commands.
#
# Usage (on the VM):
#   cd ~/FreightDesk && ./deploy.sh
set -e
cd "$(dirname "$0")"

echo "==> git pull"
git pull

echo "==> build web image"
sudo docker-compose build web

echo "==> apply pending migrations"
# ONE container for the whole step: init_db.py (creates the table if missing), then
# run_migrations.py, which applies only the scripts/migrate_*.py that have not been
# applied yet (tracked in the schema_migrations ledger). This used to be one
# `docker-compose run` per script, re-applying every migration on every deploy —
# safe, since each is idempotent, but it was most of the deploy's wall time.
#
# run_migrations.py still DISCOVERS migrations by globbing, never from a hardcoded
# list, so a newly added one is never silently skipped because someone forgot to
# register it (that caused a prod outage once). The ledger only suppresses scripts
# that have already run to completion, and a failure aborts the deploy via set -e.
sudo docker-compose run --rm web sh -c \
  'python scripts/init_db.py && python scripts/run_migrations.py'

echo "==> restart"
sudo docker-compose up -d

echo "==> deploy complete. Tailing logs (Ctrl+C to stop tailing; the app keeps running)..."
sudo docker-compose logs -f web
