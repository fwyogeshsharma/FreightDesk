# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

FreightDesk ingests trucks from three sources, OCRs their contact details (license plate,
**mobile number**, company), stores every sighting in one PostgreSQL `trucks` table, and serves
it to brokers/telecallers in a FastAPI web console so they can call drivers. **Core invariant:
only the extracted text is permanent.** Video/stream decode frames and discard the pixels
immediately. Mobile reports keep the uploaded photos in temporary storage for at most ~2 days
(for async OCR + telecaller review), then auto-delete them — the photos are never retained
long-term (this replaced an earlier "photos are never stored at all" rule).

Stack: FastAPI · SQLAlchemy 2 + psycopg3 · PostgreSQL · Jinja2 + Tailwind(CDN) + HTMX ·
Ultralytics YOLOv8 · EasyOCR · OpenCV. CPU-only. Host runs Python 3.14; the Docker image runs
Python 3.12 (widest ML-wheel availability) with CPU-only torch.

## Commands

Local dev is driven by `.bat` wrappers that call `.venv\Scripts\python.exe` directly (robust to
folder renames — never `activate` the venv). Most operational recipes live in **`COMMANDS.md`**
(VM deploy, account management, video processing on the VM, psql queries, mobile API curls).

```bat
run_webapp.bat                       :: web app -> http://localhost:8000 (auto-reloads on edit)
run.bat --input videos --sink db     :: process a folder of videos into the DB
run.bat --input "D:\clip.mp4" --sink db
run_stream.bat rtsp://host/stream    :: ingest a live camera stream
start_db.bat / stop_db.bat           :: portable Postgres (alternative to Docker)
```

Schema is created/upgraded by idempotent scripts (run them after pulling, safe to re-run):
```bat
.venv\Scripts\python.exe scripts\init_db.py
.venv\Scripts\python.exe scripts\migrate_report_fields.py
.venv\Scripts\python.exe scripts\migrate_user_accounts.py
.venv\Scripts\python.exe scripts\migrate_async_processing.py
.venv\Scripts\python.exe scripts\create_user.py create --username asha --role telecaller --name "Asha"
```

Docker (same image is both web app and pipeline): `docker compose up -d` (db + web);
`docker compose --profile pipeline run --rm pipeline` (extraction — not part of `up`).

**There is no test suite and no linter configured.** Don't claim tests pass; there are none to run.

Config: `DATABASE_URL` (default `postgresql+psycopg://postgres:postgres@localhost:5432/trucks`)
and `ADMIN_PASSWORD` (seeds/gates the admin account) are read from the environment. Tunable
extraction params (sampling FPS, YOLO confidence, OCR backend, tracker gaps) live in
`pipeline/config.py` as the `Config` dataclass — CLI flags override.

## Architecture

**Three acquisition sources → one `trucks` table** (`pipeline/db.py`, `SourceType` enum):
- `video` — MP4 files, batch (`main.py`)
- `stream` — RTSP/HTTP live camera (`pipeline/stream_runner.py`)
- `image_api` — mobile field reports via `POST /api/trucks/report` (`webapp/app.py`).
  **Asynchronous**: the endpoint stores the photos, inserts a `QUEUED` row, and returns `202`
  immediately; a background worker (`webapp/processing.py`) OCRs the photos one-at-a-time and
  updates the row. The mobile app polls `GET /api/trucks/{id}` until `processing_status` is
  `DONE`/`FAILED`. Photos live in pluggable storage (`pipeline/storage.py`: local files in dev,
  GCS bucket + 2-day lifecycle in prod) and are read back by the worker. Because the photos are
  persisted, the queue is durable — on startup the worker re-enqueues any unfinished rows.

**One shared extraction core.** `pipeline/extract.py::extract_truck_fields(event)` turns a closed
`TruckEvent` into structured fields (plate, company, phone, website, type, city) using regex over
OCR text. It holds **zero** output-format concerns (no ids, no progress, no CSV/DB shape) so all
three sources produce *identical* extraction — with one deliberate, narrow exception:
`allow_digit_fragments` (default `False`). Video/stream frames can carry a burned-in OSD clock, so
a short pure-digit OCR read is treated as noise by default. Mobile report photos have no OSD
overlay, so `webapp/processing.py` passes `allow_digit_fragments=True`, trusting a 4+ digit run as
a plausible partial plate/series read instead of discarding it — this is what lets a plate photo
that only captured one line (e.g. the bottom series+number, missing the state/RTO prefix) still
verify. Every sink implements the same duck-typed interface: `.write(event, progress) -> bool`.
Sinks: `pipeline/writer.py` (CSV), `pipeline/db_writer.py` (Postgres). `main.py::_MultiWriter` fans
one event to several sinks.

**Pipeline flow (per video/stream):** `video_sampler` (motion-gated sampling + bursts) →
`detector` (YOLOv8 vehicle gate, optional dedicated plate model under `models/`) → `ocr_engine`
(EasyOCR) → `tracker` (IoU-matches detections across frames into one `TruckEvent`, closes it when
the truck leaves) → `extract` → sink. The image API (`pipeline/image_api.py`) reuses the *same*
detect→plate→OCR chain but bypasses the tracker — it collapses up to 5 photos of one truck into a
single `TruckEvent` directly.

**Three independent status dimensions on mobile reports — keep them distinct (don't conflate):**
- `processing_status` (QUEUED / PROCESSING / DONE / FAILED): **machine job lifecycle**. Owned by
  the async worker (`webapp/processing.py`). The app polls this. NULL for video/stream.
- `verification_status` (VERIFIED / UNVERIFIED): **automatic**. Set in `pipeline/reports.py::reconcile`
  by fuzzy-matching the user-typed plate against what OCR read off the photos. Contributors are
  anonymous and paid, so nothing is trusted on their word — the photos are the only proof. Reports
  are *always stored*; failing verification just records a reason for abuse review.
- `review_status` (PENDING / PASSED / REJECTED): **human**. A telecaller's decision via
  `PATCH /api/trucks/{id}` or the `/review` queue. **PASSED = the contributor is reward-eligible.**

Every mobile report also writes a `submission_log` row (audit trail for spotting reward farming).
`require_phone=True` on the video/stream DB writer drops sightings with no callable number (a
telecaller can't act on them); mobile reports require a phone at the API layer instead.

**Auth (`pipeline/auth.py`, `webapp/app.py`):** one `users` table for everyone — external mobile
*contributors* (self-register by phone, role `contributor`) and internal *operators* (created by
admin via `scripts/create_user.py`, role `telecaller`/`admin`). Login id is phone for contributors,
username for operators. One `user_sessions` table backs **both** the web session cookie and the
mobile bearer token. `get_current_user` resolves either; an explicit `Authorization: Bearer` header
wins over an ambient cookie. Only `telecaller`/`admin` may sign into the web `/review` console.
Passwords are stdlib PBKDF2-HMAC-SHA256 (no third-party crypto dep). `auth` helpers flush but
**never commit** — the caller (request handler) owns the transaction.

**Process/engine model (important for the parallel pipeline):** the SQLAlchemy engine is process-
local and **must never cross a fork/spawn boundary**. Workers are spawned with `mp.get_context("spawn")`
and each calls `pipeline/db.py::reset_engine()` before building its own engine. In `--workers N`
mode each worker loads its own ML models and (for DB sink) writes directly, since Postgres handles
concurrent writers; only the CSV sink funnels through a parent queue to keep ids sequential. Do not
raise `--workers` on the small prod VM — it OOMs (see COMMANDS.md).

**Web surface (`webapp/app.py`, single file):** `/` broker workspace (search/filter/sort,
newest-first, click-to-call), `/review` telecaller queue (login required), JSON API under
`/api/*`, mobile auth under `/api/auth/*`. The newest-first paging query is the hot path —
`detected_at` has a descending index. ML models are owned by the background worker
(`webapp/processing.py::_Models`) and load lazily on the first queued report.
`GET /trucks/{id}/image/{idx}` streams a stored report photo from storage (within the ~2-day
window) — open to reviewers (telecaller/admin, for `/review` queue triage) and to the
contributor who submitted the report (bearer token owner match on `reported_by_user_id`, so
the mobile app can show back what was uploaded); 403 otherwise. Anonymous submissions have no
owner and can't be fetched back this way.

**`/` groups reports into broker leads; `/review` stays report-level — these are deliberately
different units.** `webapp/broker_grouping.py::group_broker_rows()` collapses repeat sightings of
the same real-world truck (matched conservatively — same normalized phone, and same normalized
plate whenever that phone maps to more than one plate, to avoid merging genuinely different
trucks that share a contact number like a dispatcher's line) into one row for the broker page.
`/review` never imports this module — one queue item is always exactly one submitted report, and
Pass/Reject acts on that report's id. Because grouping must see the whole filtered set before it
can cluster and paginate, `index()` fetches every matching row (no SQL `LIMIT/OFFSET`, no fetch
cap) and groups/sorts/paginates in Python — deliberate, since a capped fetch silently drops older
matches from an unfiltered/broad query as the table grows. This became a real perf bottleneck once
the payment-report import pushed the table past ~7000 rows (the DB query itself stays sub-second —
`detected_at` is indexed — but materializing+grouping thousands of rows in Python took 5-19s on the
small shared prod VM); rather than reintroduce a row cap, `index()` now keeps two short in-process
TTL caches (`_index_leads_cache` keyed by filter params, 20s; `_index_facets_cache` for the type/city
dropdown lists, 60s) so repeated requests for the same view reuse the last computed result instead
of recomputing from scratch — zero data cap, just staleness up to the TTL. Revisit (e.g. a
materialized lead id or DB-side grouping) if this still isn't enough as the table keeps growing. The detail slide-over
(`GET /trucks/{id}/panel`) independently re-resolves
that truck's full sibling history (via a phone/plate lookup, not the list's current filters) so
"Sighting history" is always complete regardless of what filter you had applied to find the lead.

**Async worker constraints (`webapp/processing.py`):** a single in-process thread drains an
in-memory queue and processes reports **one at a time** — deliberate, so two concurrent OCR
passes can't OOM the 2 GB prod VM. The in-memory queue belongs to ONE uvicorn worker; the VM
runs a single worker (Dockerfile CMD has no `--workers`). **Do not scale to multiple uvicorn
workers** without moving to a shared queue. Run `scripts/migrate_async_processing.py` after pulling.

## Reference docs

- **`COMMANDS.md`** — operational cheat sheet (run/deploy/operate the prod VM, account mgmt, psql).
- **`API_CONTRACT.md`** — mobile field-report API contract. Live Swagger at `/docs`.
- **`DEPLOY.md`** — setup & deployment. **`DESIGN_REVIEW.md`** — broker-console UX & scale notes.
