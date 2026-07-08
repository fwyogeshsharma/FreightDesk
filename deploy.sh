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

echo "==> apply migrations (idempotent, safe to re-run)"
# init_db.py first (creates the table if missing), then every scripts/migrate_*.py
# by filename — auto-discovered so a newly-added migration is never silently
# skipped just because this list wasn't updated (that caused a prod outage once).
echo "  -> scripts/init_db.py"
sudo docker-compose run --rm web python scripts/init_db.py
for script in scripts/migrate_*.py; do
  echo "  -> $script"
  sudo docker-compose run --rm web python "$script"
done

echo "==> restart"
sudo docker-compose up -d

echo "==> deploy complete. Tailing logs (Ctrl+C to stop tailing; the app keeps running)..."
sudo docker-compose logs -f web
