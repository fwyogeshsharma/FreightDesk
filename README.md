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
| **Mobile field reports** | `POST /api/trucks/report` — on‑road users submit ≤5 photos + form fields; **async** (returns `202`, OCR'd in the background, app polls for status); photos retained indefinitely | Telecaller review: Pending → Passed / Rejected |

**Two operational web consoles (one shared shell):**
- **Truck Sightings** (`/`) — broker workspace: search anything, filter, sort, newest‑first, one‑tap **click‑to‑call**.
- **Review Queue** (`/review`) — telecaller workspace (login required): validate field reports, Pass/Reject; Passed = contributor is reward‑eligible.

**Developer tools** (kept out of product nav): `GET /report-test` (API test console) and `GET /docs` (Swagger).

## Tech stack

FastAPI · SQLAlchemy 2 + psycopg3 · PostgreSQL · Jinja2 + Tailwind (CDN) + HTMX ·
Ultralytics YOLOv8 · EasyOCR · OpenCV · Python 3.12 (CPU‑only).

## Run locally (Windows)

The web app runs on your machine from a Python **3.12** venv; only PostgreSQL runs in Docker.
(The host's own Python is 3.14, which is too new for the torch/EasyOCR wheels — hence 3.12.)

### Every time

```bat
:: 1. Start Docker Desktop and wait for it to say "Engine running"

:: 2. Start Postgres
docker compose up -d db

:: 3. Apply any new schema changes (instant when there are none — safe every time)
.venv\Scripts\python.exe scripts\init_db.py
.venv\Scripts\python.exe scripts\run_migrations.py

:: 4. Start the web app (auto-reloads when you edit code or templates; Ctrl+C to stop)
run_webapp.bat
```

Then open:

| Page | URL |
|---|---|
| Truck Sightings (broker) | http://localhost:8000 |
| Review Queue (telecaller) | http://localhost:8000/review — log in as `admin` with the `ADMIN_PASSWORD` from `.env` |
| Submit a test field report | http://localhost:8000/report-test |
| API docs (Swagger) | http://localhost:8000/docs |

Step 3 matters after every `git pull`: if the code expects a column your local database doesn't
have yet, every page fails until the migration runs.

When you're done: `Ctrl+C` in the web app window, then `docker compose stop db`.

### First time only

```bat
:: Python 3.12 venv. Call its python directly - never `activate` it (the activate
:: script bakes in an absolute path and breaks if the folder is ever moved/renamed).
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
.venv\Scripts\python.exe -m pip install -r requirements.txt

copy .env.example .env
```

In `.env`, keep **`127.0.0.1`** (not `localhost`) in `DATABASE_URL`. On Windows `localhost` resolves
to IPv6 first while Docker publishes Postgres on IPv4 only, so a `localhost` URL hangs the app at
*"Waiting for application startup"* with no error. Also set `ADMIN_PASSWORD`; the `admin` account is
created with it on the first start.

Install CPU-only torch **before** `requirements.txt` (as above), or pip pulls the multi-GB CUDA build.

### Troubleshooting

| Symptom | Cause |
|---|---|
| `failed to connect to the docker API` | Docker Desktop isn't running (step 1) |
| App stuck on *"Waiting for application startup"* | `DATABASE_URL` uses `localhost` — change it to `127.0.0.1` |
| Pages return 500 after a `git pull` | A migration hasn't run — repeat step 3 |
| A test report sits in "Processing" | The first report after starting loads the OCR models, which takes a few minutes |

Processing videos, creating operator accounts and the rest of the day-to-day commands are in
**`COMMANDS.md`**.

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
