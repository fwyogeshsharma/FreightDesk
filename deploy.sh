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
for script in scripts/init_db.py \
              scripts/migrate_report_fields.py \
              scripts/migrate_user_accounts.py \
              scripts/migrate_async_processing.py \
              scripts/migrate_body_type.py; do
  echo "  -> $script"
  sudo docker-compose run --rm web python "$script"
done

echo "==> restart"
sudo docker-compose up -d

echo "==> deploy complete. Tailing logs (Ctrl+C to stop tailing; the app keeps running)..."
sudo docker-compose logs -f web
