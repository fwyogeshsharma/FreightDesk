# Truck Data Platform — Setup & Deployment

This project now has three layers:

1. **Pipeline** — extracts trucks from videos / live streams / uploaded photos
   (`main.py`, `pipeline/`). CPU-heavy; runs on the host where the videos live.
2. **Database** — PostgreSQL `trucks` table; one row per sighting, newest-first.
3. **Web app** — broker UI + JSON API + still-image API (`webapp/`).

`DATABASE_URL` wires everything together (default
`postgresql+psycopg://postgres:postgres@localhost:5432/trucks`).

**Set `ADMIN_PASSWORD`** before deploying — it protects the telecaller review page
(`/review`) and the review PATCH endpoint, which decide reward eligibility. If unset it
falls back to `admin` (logged as a warning). Optional `ADMIN_USER` (default `admin`).

---

## A. Local development with Docker (recommended)

Prerequisite: **Docker Desktop** (install once: `winget install Docker.DockerDesktop`,
then reboot). The same compose file deploys later to a VM.

```powershell
copy .env.example .env          # adjust passwords if you like

# Just the database (run the pipeline + app on the host):
docker compose up -d db

# Create / upgrade the schema (from the host venv):
.venv\Scripts\python.exe scripts\init_db.py
.venv\Scripts\python.exe scripts\migrate_report_fields.py    # field-report columns
.venv\Scripts\python.exe scripts\migrate_user_accounts.py    # users + attribution columns
.venv\Scripts\python.exe scripts\migrate_async_processing.py # async processing + photo-storage columns

# Process videos into the DB (+ keep the CSV as a backup):
run.bat --input videos --sink both          # or --sink db
# ...or upload photos / point a stream at it (see below).

# Broker app on the host:
run_webapp.bat                               # http://localhost:8000
```

Run **everything** in containers (DB + web app):

```powershell
docker compose up -d            # db + web; app at http://localhost:8000
```

The `web` container talks to the DB at host `db` (set automatically).

### Processing videos (where the footage comes from)

**Videos are operational input data, not code — they are never committed to git or baked
into the Docker image.** You supply them at runtime. Two options:

**On the host** (simplest; the pipeline is CPU-heavy and the footage is usually local):
```powershell
run.bat --input "D:\path\to\footage" --sink db     # DATABASE_URL → localhost:5432
```

**In a container** (mounts the host footage folder read-only via `VIDEOS_DIR`):
```powershell
# point VIDEOS_DIR at wherever the videos live; default is ./videos
$env:VIDEOS_DIR = "D:\path\to\footage"
docker compose --profile pipeline run --rm pipeline                  # --sink db by default
docker compose --profile pipeline run --rm pipeline --workers 2 --sink both   # extra args
```
The `pipeline` service shares the compose network, so it reaches Postgres at `db:5432`
automatically. It is profile-gated, so a plain `docker compose up` never starts it.

> On a VM, set `VIDEOS_DIR` to the directory where you've copied/mounted the footage
> (e.g. an attached disk or NFS share). The DB persists in the `pgdata` volume.

---

## Production on a cloud VM (Docker + automatic HTTPS)

Run FreightDesk publicly on a Linux VM (GCP/AWS/etc.). Caddy fronts the app and
auto-provisions a free Let's Encrypt TLS certificate. Postgres and the raw app port stay
private (localhost-only); only 80/443 are exposed.

**Prerequisites:** a Linux VM with a public IP, SSH access, and a **domain or free
subdomain** pointing at the VM's IP (a `DuckDNS` name like `freightdesk.duckdns.org` works
and gets a real cert).

```bash
# 1. SSH in, then install Docker (Debian/Ubuntu/most distros)
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker          # run docker without sudo

# 2. Point your domain's DNS A-record at the VM's external IP (e.g. DuckDNS / registrar),
#    and open the firewall for HTTP+HTTPS:
#    GCP:  gcloud compute firewall-rules create freightdesk-web \
#            --allow tcp:80,tcp:443 --source-ranges 0.0.0.0/0
#    (SSH/22 is already allowed. Do NOT open 5432 or 8000.)

# 3. Get the code + configure
git clone https://github.com/fwyogeshsharma/FreightDesk.git && cd FreightDesk
cp .env.example .env
nano .env        # set: DOMAIN=<your domain>, a strong POSTGRES_PASSWORD, a strong
                 #      ADMIN_PASSWORD, and DATABASE_URL to match POSTGRES_PASSWORD

# 4. Build & start db + web + caddy (HTTPS)
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build

# 5. Create the database schema (one-off, inside the web image)
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm web python scripts/init_db.py
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm web python scripts/migrate_report_fields.py
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm web python scripts/migrate_user_accounts.py
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm web python scripts/migrate_async_processing.py
```

### Mobile-report photos — async processing & GCS storage (≤2-day retention)

`POST /api/trucks/report` is **asynchronous**: it accepts the report, stores the photos, and
returns `202` immediately; a background worker OCRs them and the app polls `GET /api/trucks/{id}`
until `processing_status` is `DONE` (see `API_CONTRACT.md`). Photos are kept **at most ~2 days**
(for OCR + telecaller review), then deleted — only the extracted text is permanent.

In prod, store the photos in a **GCS bucket whose lifecycle rule enforces the 2-day expiry** (so
deletion is guaranteed by GCP, and the 2 GB VM's disk is never used for images). One-time setup
(run from your laptop with `gcloud`, or Cloud Shell):

```bash
# 1. Create a private bucket in the same region as the VM
gcloud storage buckets create gs://freightdesk-report-photos \
  --project=agile-airship-198614 --location=us-central1 --uniform-bucket-level-access

# 2. Auto-delete objects 2 days after creation (the retention requirement, enforced by GCP)
printf '{"rule":[{"action":{"type":"Delete"},"condition":{"age":2}}]}' > /tmp/lifecycle.json
gcloud storage buckets update gs://freightdesk-report-photos --lifecycle-file=/tmp/lifecycle.json

# 3. Let the VM's service account read/write the bucket (no key files — uses the VM identity).
#    Find the VM service account: GCP Console → VM → "Service account", or:
#    gcloud compute instances describe rolling-expense-prod --zone=us-central1-a \
#      --format="value(serviceAccounts[0].email)"
gcloud storage buckets add-iam-policy-binding gs://freightdesk-report-photos \
  --member="serviceAccount:<VM_SERVICE_ACCOUNT_EMAIL>" --role=roles/storage.objectAdmin
```

Then point the app at it via the VM's `.env` and restart:

```bash
IMAGE_STORAGE_BACKEND=gcs
GCS_BUCKET=freightdesk-report-photos
# IMAGE_RETENTION_DAYS is informational here — GCS lifecycle does the actual deleting.
```

> The web container authenticates to GCS with the VM's own service account (Application Default
> Credentials) — **no JSON key file needed**. Locally, leave `IMAGE_STORAGE_BACKEND=local` (plain
> files under `./uploads`, swept after 2 days) so you don't need GCS to develop.
>
> **Single-worker only:** the OCR queue lives in one process. Keep uvicorn at one worker on the VM
> (the default). Scaling workers needs a shared queue first.

> `migrate_user_accounts.py` adds the `users` + `user_sessions` tables and the reporter/
> reviewer attribution columns, then seeds an admin from `ADMIN_USERNAME`/`ADMIN_PASSWORD`.
> It's idempotent — safe to re-run after a `git pull`.

Now **`https://<your-domain>`** serves the broker console; `/review` is the telecaller
console (Telecaller Login = `ADMIN_PASSWORD`); the mobile app posts to
`https://<your-domain>/api/trucks/report`.

**Updates:** `git pull && docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build`.
**Logs:** `docker compose logs -f web caddy`. **Cert issues:** check `docker compose logs caddy`
(needs DNS pointing at the VM and ports 80/443 reachable).

### Changing the domain later (config-only — no rebuild)

The public hostname is just the `DOMAIN` env var; nothing in the app code hardcodes it
(all links are relative). To move from the free DuckDNS subdomain to a purchased domain:

1. Point the new domain's DNS **A-record** at the same VM IP (e.g. `app.yourco.com → 34.31.185.19`).
2. Edit `.env`: `DOMAIN=app.yourco.com`.
3. `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d` — Caddy fetches a
   fresh Let's Encrypt cert for the new name automatically.

**Zero-downtime cutover:** serve both names during the transition — Caddy accepts a
space-separated list — then drop the old one later:
```
DOMAIN="freightdesk.duckdns.org app.yourco.com"
```

The only place the hostname lives *outside* FreightDesk is the **mobile app's API base URL**
(`https://<domain>/api/trucks/report`). Point it at the new domain once you switch; the
dual-domain setup keeps the old URL working until you do.

**Video extraction on the VM** (separate, on demand — copy footage to the VM first):
```bash
VIDEOS_DIR=/path/to/footage docker compose --profile pipeline \
  -f docker-compose.yml -f docker-compose.prod.yml run --rm pipeline --sink db
```

---

## B. Local without Docker (portable Postgres fallback)

If you don't want Docker locally, `start_db.bat` runs a self-contained PostgreSQL
from `pgsql/` against a data dir in `pgdata/` (no admin, no service):

```powershell
start_db.bat                                 # starts Postgres on :5432
.venv\Scripts\python.exe scripts\init_db.py
run_webapp.bat
```

---

## Inputs

| Source | Command |
|---|---|
| Video files | `run.bat --input videos --sink db` (add `--workers 2` for two in parallel) |
| Live stream | `run_stream.bat rtsp://user:pass@host:554/stream` |
| Mobile field report | `POST /api/trucks/report` (see contract below) |

## API quick reference

- `GET  /` — broker web UI (search, newest-first, click-to-call) — **public**
- `GET  /api/trucks?q=&source=&limit=&offset=` — JSON list
- `GET  /api/trucks/{id}` — JSON detail (incl. `verification_status`, `review_status`)
- `POST /api/trucks/report` — mobile field report → one truck record (JSON)
- `POST /api/auth/register` — mobile contributor self-registration → bearer token
- `POST /api/auth/login` — mobile login → bearer token
- `GET  /api/auth/me` — current account (bearer)
- `POST /api/auth/logout` — revoke the bearer session
- `PATCH /api/trucks/{id}` — telecaller review decision (reviewer auth)
- `GET  /review` — telecaller review queue (web login)

### `POST /api/trucks/report` — contract for the mobile app

`multipart/form-data`. **Asynchronous** — returns `202` immediately; the app polls
`GET /api/trucks/{id}` for `processing_status` (full contract in `API_CONTRACT.md`). Photos are
stored ≤2 days for OCR + review, then auto-deleted.

| Field | Type | Required | Notes |
|---|---|---|---|
| `images` | file ×1–5 | **yes** | photos of ONE truck; max 5 |
| `phone_number` | string | **yes** | phone read on the truck |
| `vehicle_number` | string | no | plate read by the user |
| `loaded_status` | string | no | `loaded` / `unloaded` |
| `number_of_wheels` | int | no | |
| `location` | string | no | address / place text |
| `latitude`, `longitude` | float | no | GPS |
| `captured_at` | ISO-8601 | no | defaults to server time |
| `reported_by` | string | no | app user id/name |

Example:
```
curl -F images=@a.jpg -F images=@b.jpg \
     -F phone_number=9811008120 -F vehicle_number=RJ14CA1234 \
     -F loaded_status=loaded -F number_of_wheels=12 \
     -F location="NH-48 Jaipur" -F latitude=26.91 -F longitude=75.78 \
     http://localhost:8000/api/trucks/report
```

Responses:
- `202` → `{ "id": n, "processing_status": "QUEUED", "review_status": "PENDING", "images_accepted": n, "status_url": "/api/trucks/n" }` — then poll `status_url` until `processing_status` is `DONE`/`FAILED`.
- `400` → blank phone / more than 5 images / no decodable image; `422` → missing required field.

**Trust model (contributors are anonymous and paid for correct uploads — never trusted
on their word):** the request is **always stored**, but only `verification_status =
VERIFIED` when the photos confirm the typed `vehicle_number`. No plate readable, no
number typed, or a conflicting plate → stored as **UNVERIFIED** with a `reason` (shown
with an "⚠ Unverified" badge; not auto-trusted/paid). The typed `phone_number` is merged
with any phone OCR'd from the photos (`phone_reported` / `phone_ocr` keep provenance).

**Abuse audit:** every submission is recorded in `submission_log` (reporter, status,
reason, reported-vs-OCR values, image count, linked truck id). Find contributors
gaming rewards:
```sql
SELECT reported_by, count(*) total,
       count(*) FILTER (WHERE status='UNVERIFIED') unverified
FROM submission_log GROUP BY reported_by ORDER BY unverified DESC;
```

### User accounts & authentication

One unified `users` table serves two audiences, distinguished by `role`:

| Role | Who | Created how | Access |
|---|---|---|---|
| `contributor` | external mobile uploaders | self-register in the app | submit reports |
| `telecaller` | internal reviewers | **admin, via CLI** | the `/review` queue + PATCH |
| `admin` | operators | seeded from env / CLI | everything + manage users |

Auth is **opaque session tokens** (the `user_sessions` table) — the **same tokens** back
the mobile **bearer** header and the web **session cookie**. Passwords are stored as
stdlib PBKDF2-SHA256 (no third-party crypto). **Login id depends on audience:**
contributors log in by **phone** (mobile); telecallers/admins log in by **username** (web).

**Mobile API (the app's register/login screens):**
```bash
# register (always a contributor) -> {token, user}
curl -X POST -H "Content-Type: application/json" \
     -d '{"phone":"9811008120","password":"secret123","display_name":"Ravi"}' \
     https://<host>/api/auth/register

# login -> {token, user};  then send the token on every call:
curl -H "Authorization: Bearer <token>" https://<host>/api/auth/me
# attributed report: add  -H "Authorization: Bearer <token>"  to POST /api/trucks/report
```
Report submission is **transition mode**: with a valid bearer token the row is attributed
to that user (`reported_by_user_id` + `reporter_phone`/name snapshots); without one it
stays anonymous exactly as before — the app can adopt auth at its own pace.

**Internal operators (telecallers/admins) are created by an admin — no self-service.**
They log in by **username**; `--phone` is an optional contact field.
```bash
# in a container (or host venv): create / manage operator accounts
docker compose run --rm web python scripts/create_user.py create --username asha --role telecaller --name "Asha"
docker compose run --rm web python scripts/create_user.py list
docker compose run --rm web python scripts/create_user.py set-role  --user asha --role admin
docker compose run --rm web python scripts/create_user.py set-password --user asha
docker compose run --rm web python scripts/create_user.py deactivate --user asha
```
The **seed admin** is created automatically on first start from `ADMIN_USERNAME`
(default = `ADMIN_USER`, i.e. `admin`) + `ADMIN_PASSWORD` — log into the web app with that
**username** + password, then add telecallers with the CLI. Set `SECURE_COOKIES=1` once
HTTPS fronts the app. The legacy `X-Admin-Token: <ADMIN_PASSWORD>` still authenticates the
PATCH endpoint for automation.

### Telecaller review & rewards

Two statuses per report: **`verification_status`** (machine, from photos:
VERIFIED/UNVERIFIED) and **`review_status`** (human telecaller: PENDING → PASSED /
REJECTED). Every report starts PENDING.

- Telecallers open **`/review`** (log in with their **username + password**), work the
  **pending** queue, call the number to confirm the truck is real, then **Pass**/**Reject**.
- Programmatic equivalent — `PATCH /api/trucks/{id}` with header `X-Admin-Token: <ADMIN_PASSWORD>`:
  ```
  curl -X PATCH -H "X-Admin-Token: $ADMIN_PASSWORD" -H "Content-Type: application/json" \
       -d '{"review_status":"passed","review_note":"confirmed by call"}' \
       http://localhost:8000/api/trucks/123
  ```
- **Reward-eligible contributors** (PASSED reports):
  ```sql
  SELECT reported_by, count(*) approved
  FROM trucks WHERE source='image_api' AND review_status='PASSED'
  GROUP BY reported_by;
  ```

## Verify

```powershell
.venv\Scripts\python.exe scripts\verify_db.py   # inserts rows, exercises the app + image API
```
