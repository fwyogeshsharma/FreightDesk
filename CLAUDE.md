# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

FreightDesk ingests trucks from three sources, OCRs their contact details (license plate,
**mobile number**, company), stores every sighting in one PostgreSQL `trucks` table, and serves
it to brokers/telecallers in a FastAPI web console so they can call drivers. **Video and stream
frames are never persisted** — they are decoded, OCR'd, and the pixels discarded immediately; for
those two sources only the extracted text survives. **Mobile report photos are different: they
are now retained indefinitely.** This rule has moved twice — "photos are never stored at all" →
"stored ~2 days, then auto-deleted" → (2026-09-14) "kept forever", to allow re-OCR with better
models, abuse investigation, and training data. Much of the repo's older prose still says photos
expire; treat that as stale and correct it when you touch it.

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

Local startup (full walkthrough + troubleshooting in **README.md → Run locally**): Docker Desktop
running → `docker compose up -d db` → migrations (below) → `run_webapp.bat`. `DATABASE_URL` in `.env`
must use `127.0.0.1`, never `localhost` (IPv6-first resolution hangs startup against Docker's
IPv4-only port). Reviewer login is `admin` + `.env`'s `ADMIN_PASSWORD`.

Schema is created/upgraded by idempotent scripts (run them after pulling, safe to re-run):
```bat
.venv\Scripts\python.exe scripts\init_db.py
.venv\Scripts\python.exe scripts\run_migrations.py      :: pending migrate_*.py only (--list, --all)
.venv\Scripts\python.exe scripts\create_user.py create --username asha --role telecaller --name "Asha"
```
`run_migrations.py` **discovers** `scripts\migrate_*.py` by glob — never a hardcoded list (a
forgotten registration once caused a prod outage) — and records each in a `schema_migrations`
ledger only after it exits 0, so already-applied ones are skipped. `deploy.sh` uses it too, in one
container. Consequently **only real schema migrations may be named `migrate_*.py`**: anything
matching the glob runs unattended on every fresh database and on the next prod deploy (one-off
housekeeping like `rekey_report_photos_date_first.py` is deliberately named otherwise). A migration
adding a column must land before the code that selects it is served — the ORM selects every mapped
column, so a missing one 500s every page; `deploy.sh` migrates before restarting for this reason.

Docker (same image is both web app and pipeline): `docker compose up -d` (db + web);
`docker compose --profile pipeline run --rm pipeline` (extraction — not part of `up`).

**There is no test suite and no linter configured.** Don't claim tests pass; there are none to run.
The one smoke check is `scripts\verify_db.py` — against a live Postgres it inserts synthetic rows,
drives the FastAPI app through `TestClient`, and runs the still-image API on a real video frame.
It mutates the target DB, so point `DATABASE_URL` at a scratch database, never prod.

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
  a GCS bucket in prod) and are read back by the worker. Because the photos are persisted, the
  queue is durable — on startup the worker re-enqueues any unfinished rows.

**Photo expiry is bucket-level in prod, and there is no code path that enforces it.** Nothing in
the app checks a photo's age: `GET /trucks/{id}/image/{idx}` simply 404s if the object is missing,
and `image_keys` is never cleared. `GCSStorage.purge_expired()` is a hard `return 0`, so on the
`gcs` backend `IMAGE_RETENTION_DAYS` is inert in both directions — it cannot delete anything, and
a bucket lifecycle rule would delete objects no matter what it says. Retention in prod is changed
only with `gcloud storage buckets update --lifecycle-file=...` (or `--clear-lifecycle`); the prod
bucket has no rule today. `LocalStorage.purge_expired()` *does* delete, but only when
`IMAGE_RETENTION_DAYS > 0`; it defaults to `0` (keep forever) and `webapp/processing.py` doesn't
even start the sweeper thread unless a positive value is set on the local backend. **Don't add an
age check in app code** — it would put expiry in two places that can silently disagree.

**`source = image_api` does not always mean "a mobile report that went through OCR."** Two one-time
bulk imports — `scripts/import_legacy_field_survey.py` (a team's manually-collected survey sheet)
and `scripts/import_payment_report.py` (~3500 trips from an external ops/payment system) — also
write `image_api` rows, because that's what puts them on the `/` broker page without a schema
change. They are inserted already `VERIFIED`/`PASSED`/`DONE` so they bypass the telecaller `/review`
queue (which exists to triage *unreviewed paid contributor* submissions), and they typically have no
`image_keys` and no OCR provenance. So don't assume an `image_api` row has photos, a
`processing_status` history, or a `reported_by_user_id`. The payment import is also what pushed the
table past ~7000 rows (see the `/` caching note below); it fans one multi-leg trip into one row per
distinct place on the route so a broker searching any stop finds the truck.

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

**Two small enrichment modules feed the broker view and are easy to miss:**
- `pipeline/timestamps.py` — `detected_at` for a video sighting is **not** ingest time. It parses the
  14-digit DVR timestamp out of the filename (`D01_20230331124308.mp4`) and adds the frame offset,
  so "newest first" reflects when the truck was actually filmed. Falls back to `now()` for streams,
  image uploads, and any filename without that pattern — so re-processing an unstamped old video
  makes it look brand new on `/`.
- `pipeline/geocode.py` — OpenStreetMap Nominatim, used **both ways** on mobile reports from
  `pipeline/reports.py::reconcile`, each direction covering the other's missing half: coords → place
  name when the app sends only GPS, and place name → coords when the reporter typed a location but
  the app sent none (the Android "show on map" only reads lat/lng, so NULL coords break it). It's a
  live third-party HTTP call with a ~1 req/sec usage cap — safe only because at most one direction
  fires per report and it's already serialized behind the single OCR worker thread. Don't move it
  into a parallel path or the request handler.

**Three independent status dimensions on mobile reports — keep them distinct (don't conflate):**
- `processing_status` (QUEUED / PROCESSING / DONE / FAILED): **machine job lifecycle**. Owned by
  the async worker (`webapp/processing.py`). The app polls this. NULL for video/stream.
- `verification_status` (VERIFIED / UNVERIFIED): **automatic**. Set in `pipeline/reports.py::reconcile`
  by fuzzy-matching the user-typed plate against what OCR read off the photos. Contributors are
  anonymous and paid, so nothing is trusted on their word — the photos are the only proof. Reports
  are *always stored*; failing verification just records a reason for abuse review.
- `review_status` (PENDING / PASSED / REJECTED): **human**. A telecaller's decision via
  `PATCH /api/trucks/{id}` or the `/review` queue. **PASSED = the contributor is reward-eligible.**

**Reviewers can correct a report's fields before deciding** (`/review` → Edit, backed by
`PATCH /api/trucks/{id}/fields`, a separate endpoint from the Pass/Reject PATCH). Only a
whitelisted set of typed fields is editable (`_EDITABLE_FIELDS` in `webapp/app.py`); provenance,
GPS, photos and all machine output are not. Three rules, all enforced server-side (409), not just
by hiding the button: (1) only while `review_status` is PENDING — a decision is made on the data
as it stood; (2) never while `processing_status` is QUEUED/PROCESSING, because the worker snapshots
the typed fields when it *starts* (`reported_from_row`) and writes them all back when it finishes
(`finalize_report`), silently overwriting any edit made in between; (3) **`verification_status` is
not recomputed** — it stays the machine's verdict on what the contributor actually submitted, so a
telecaller "fixing" a wrong plate can't launder an UNVERIFIED report into a VERIFIED, paid one.
Every change is appended to `trucks.edit_history` (JSONB: who, when, field → [old, new]) because the
edit overwrites the contributor's value in place; `phone_reported` also keeps their original number.
One gap to know about: requeuing an edited report re-runs `reconcile` on the *edited* plate as if the
contributor had typed it — `edit_history` is then the only record of what they actually sent.

Every mobile report also writes a `submission_log` row (audit trail for spotting reward farming).
`require_phone=True` on the video/stream DB writer drops sightings with no callable number (a
telecaller can't act on them). Mobile reports don't have an equivalent gate — as of 2026-09-15
`phone_number` is optional on `POST /api/trucks/report`; a report submitted with none is still
stored and still goes through review, it just has no callable number (same practical effect as a
video/stream sighting that failed `require_phone`, just not dropped since a human already typed
the rest of the report). `phone_number` blank is still checked against the `users` table for a
blocked (`is_active=False`) account when one *is* given — see the auth section above.

**Auth (`pipeline/auth.py`, `webapp/app.py`):** one `users` table for everyone — external mobile
*contributors* (self-register by phone, role `contributor`) and internal *operators* (created by
admin via `scripts/create_user.py`, role `telecaller`/`admin`). Login id is phone for contributors,
username for operators. One `user_sessions` table backs **both** the web session cookie and the
mobile bearer token. `get_current_user` resolves either; an explicit `Authorization: Bearer` header
wins over an ambient cookie. Only `telecaller`/`admin` may sign into the web `/review` console.
Passwords are stdlib PBKDF2-HMAC-SHA256 (no third-party crypto dep). `auth` helpers flush but
**never commit** — the caller (request handler) owns the transaction.

**`is_active` gates everything session-based, and self-registered contributors start with it
`False`.** `pipeline/auth.py::create_user()` defaults `is_active=True` (an admin creating an
operator via `scripts/create_user.py` has already vetted them, and `ensure_seed_admin`'s bootstrap
account needs to work immediately) — the one caller that overrides it is `POST /api/auth/register`,
which passes `is_active=False` so a new contributor needs an admin to approve them
(`PATCH /api/admin/users/{id}` with `{"is_active": true}`, admin-role only — see `require_admin`)
before the account is usable. Enforcement is centralized, not scattered: `authenticate()` and
`resolve_session()` both re-check `is_active` on every call, so a pending/blocked account can never
log in and an *existing* session token stops resolving on its very next request the moment
`is_active` flips to `False` — no separate revocation step. The one endpoint that doesn't sit
behind session auth at all, `POST /api/trucks/report` (see below), explicitly re-checks the typed
`phone_number` against `users` for this same reason — otherwise a pending/blocked account could
just drop its token and submit anonymously, since the endpoint has no hard login requirement.

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
`GET /trucks/{id}/image/{idx}` streams a stored report photo from storage (404 only for photos
uploaded before the 2026-09-14 retention change, which the old rule already deleted) — open to
reviewers (telecaller/admin, for `/review` queue triage) and to the
contributor who submitted the report (bearer token owner match on `reported_by_user_id`, so
the mobile app can show back what was uploaded); 403 otherwise. Anonymous submissions have no
owner and can't be fetched back this way. `GET /api/auth/me/reports` is the contributor-facing
history (own submissions + their review outcome — the reward-status screen in the app).
`GET /report-test` renders `webapp/templates/report_test.html`, a plain browser form for submitting
a field report end-to-end; it exists because Swagger UI can't do multi-file upload reliably, so it
is the practical way to exercise the whole upload→queue→OCR→verify path by hand. Jinja templates
live in `webapp/templates/` (`index`, `detail_panel`, `review`, `login`, `report_test`, `_nav`);
Tailwind is the CDN build and HTMX drives the slide-over and queue actions, so there is no
front-end build step — edit the template and reload.

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
workers** without moving to a shared queue.

`FAILED` is terminal: `recover_pending()` only re-queues QUEUED/PROCESSING rows on startup. To retry
reports that failed for a transient reason, run `scripts/requeue_failed_reports.py` (dry run, then
`--apply`) and **restart the web container** — the script runs in its own container, so it can only
flip rows to QUEUED; the running worker's in-memory queue only learns about them on restart. If
reports sit QUEUED with *nothing* PROCESSING, the worker isn't running: check the logs for
`OCR worker started` (Sept 2026 outage: a startup exception skipped `start_worker()` for 8 days).

## Reference docs

- **`COMMANDS.md`** — operational cheat sheet (run/deploy/operate the prod VM, account mgmt, psql).
- **`API_CONTRACT.md`** — mobile field-report API contract. Live Swagger at `/docs`.
- **`DEPLOY.md`** — setup & deployment. **`DESIGN_REVIEW.md`** — broker-console UX & scale notes.
