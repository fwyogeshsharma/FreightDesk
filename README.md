# FreightDesk

Truck intelligence & dispatch platform. FreightDesk ingests trucks from multiple sources,
extracts their contact details (license plate, **mobile number**, company, etc.), stores
every sighting in PostgreSQL, and presents it to brokers and telecallers in a clean web
console so they can call drivers and arrange loads.

## What it does

**Three acquisition pipelines → one database:**

| Source | How | Trust |
|---|---|---|
| **Video files** | Road‑camera MP4s → YOLOv8 vehicle gate → EasyOCR → stored **only if a phone number is found** | Auto Verified |
| **Live stream** | RTSP/HTTP camera → same detect → OCR chain (wall‑clock timestamps) | Auto Verified |
| **Mobile field reports** | `POST /api/trucks/report` — on‑road users submit ≤5 photos + form fields; **async** (returns `202`, OCR'd in the background, app polls for status); photos kept ≤2 days then auto‑deleted | Telecaller review: Pending → Passed / Rejected |

**Two operational web consoles (one shared shell):**
- **Truck Sightings** (`/`) — broker workspace: search anything, filter, sort, newest‑first, one‑tap **click‑to‑call**.
- **Review Queue** (`/review`) — telecaller workspace (login required): validate field reports, Pass/Reject; Passed = contributor is reward‑eligible.

**Developer tools** (kept out of product nav): `GET /report-test` (API test console) and `GET /docs` (Swagger).

## Tech stack

FastAPI · SQLAlchemy 2 + psycopg3 · PostgreSQL · Jinja2 + Tailwind (CDN) + HTMX ·
Ultralytics YOLOv8 · EasyOCR · OpenCV · Python 3.14 (CPU‑only).

## Quick start

```bash
# 1. install deps
python -m venv .venv && .venv/Scripts/activate    # Windows
pip install -r requirements.txt

# 2. PostgreSQL — Docker (recommended) or local
docker compose up -d db                            # or: start_db.bat (portable)
copy .env.example .env                             # set ADMIN_PASSWORD, DATABASE_URL

# 3. create the schema
python scripts/init_db.py
python scripts/migrate_report_fields.py

# 4. ingest + serve
run.bat --input videos --sink db                   # process videos into the DB
run_webapp.bat                                      # http://localhost:8000
```

`DATABASE_URL` (default `postgresql+psycopg://postgres:postgres@localhost:5432/trucks`)
and `ADMIN_PASSWORD` (gates the telecaller review console) are read from the environment.

## Layout

```
main.py                 CLI: process videos → CSV and/or PostgreSQL  (--sink csv|db|both)
pipeline/               detection, OCR, tracking, extraction, DB writer, stream runner
webapp/                 FastAPI app + Jinja2 templates (broker, review, login, dev)
scripts/                init_db, migrate, verify
docker-compose.yml      postgres + web
```

## More docs

- **`DEPLOY.md`** — setup & deployment (Docker / portable Postgres), commands, admin auth.
- **`API_CONTRACT.md`** — the mobile field‑report API contract.
- **`DESIGN_REVIEW.md`** — broker‑console UX design & scale roadmap.
- **`session_log.md`** — full development log.
