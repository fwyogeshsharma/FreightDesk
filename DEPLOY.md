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

# Create the table (from the host venv):
.venv\Scripts\python.exe scripts\init_db.py

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

The `web` container talks to the DB at host `db` (set automatically). The pipeline
still runs on the host because it needs the local video files and CPU; it writes to
the same Postgres on `localhost:5432`.

### Deploying to the VM later

1. Install Docker on the VM.
2. Copy the repo (or push the built `web` image to a registry and pull it).
3. `docker compose up -d` — now anyone on the network can reach the app at
   `http://<vm-host>:8000`. Move the `pgdata` volume to persist data.

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

- `GET  /` — broker web UI (search, newest-first, click-to-call)
- `GET  /api/trucks?q=&source=&limit=&offset=` — JSON list
- `GET  /api/trucks/{id}` — JSON detail (incl. `verification_status`, `review_status`)
- `POST /api/trucks/report` — mobile field report → one truck record (JSON)
- `PATCH /api/trucks/{id}` — telecaller review decision (admin auth)
- `GET  /review` — telecaller review queue (admin login)

### `POST /api/trucks/report` — contract for the mobile app

`multipart/form-data`. **Images are processed (OCR) but never stored.**

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
- `200` → `{ "truck": {…}, "images_processed": n, "verification_status": "VERIFIED|UNVERIFIED", "reason": "...", "plate_status": "VERIFIED|MISMATCH|REPORTED|OCR_ONLY|NONE", "phone_status": "MATCH|MERGED|REPORTED_ONLY" }`
- `400` → blank phone / more than 5 images; `422` → missing required field.

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

### Telecaller review & rewards

Two statuses per report: **`verification_status`** (machine, from photos:
VERIFIED/UNVERIFIED) and **`review_status`** (human telecaller: PENDING → PASSED /
REJECTED). Every report starts PENDING.

- Telecallers open **`/review`** (log in with `ADMIN_PASSWORD`), work the **pending**
  queue, call the number to confirm the truck is real, then click **Pass** or **Reject**.
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
