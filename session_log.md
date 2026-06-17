# Extractor Project — Session Log

## Session Date: 2026-06-08 / 2026-06-09

---

## What Was Built

A Python pipeline (`D:\projects\Extractor\`) that:
- Reads road-camera MP4 videos of trucks
- Detects trucks using YOLOv8n (COCO model)
- Extracts all visible text (license plates, company names, fleet IDs) using EasyOCR
- Groups detections by individual truck across frames
- Writes results to a single CSV file incrementally as each truck passes

---

## Input

| Item | Detail |
|---|---|
| Location | `D:\projects\Extractor\videos\` |
| Files | 5 × MP4, ~1 GB each |
| Recorded | 2023-03-31, ~30 min per file |
| Camera | Fixed road camera, 2592×1944 resolution @ 25fps |
| Content | Indian road traffic — trucks visible throughout |

---

## Output

| File | Description |
|---|---|
| `D:\projects\Extractor\output\trucks.csv` | One row per truck event — written in real time |
| `D:\projects\Extractor\extractor.log` | Full pipeline run log with timestamps |
| `D:\projects\Extractor\output\debug\` | Vehicle crop JPGs (only when `--debug` flag used) |

### CSV columns
```
truck_id, video_file, first_seen, last_seen, frames, license_plate, plate_confidence,
company_name, phone_number, website, vehicle_type, city, other_text,
elapsed_time, video_pct, est_remaining
```
- `truck_id` — sequential event id (`T0001`, `T0002`, ...) unique within a run; identification data lives in the other columns
- `first_seen` / `last_seen` — timestamps in HH:MM:SS within the video
- `frames` — how many frames this truck appeared in
- `plate_confidence` — HIGH (≥3 frames confirmed), LOW (1–2), NONE
- `company_name` — best-effort company/organization name from truck body text
- `phone_number` — Indian mobile numbers (10 digits starting 6–9; OCR digit-confusions like i→1 auto-corrected)
- `website` — URLs found on the truck
- `vehicle_type` — from painted text (SCHOOL BUS, TANKER...) or YOLO class fallback (TRUCK/BUS)
- `city` — known Indian city names painted on the truck
- `other_text` — remaining clean text not claimed by the columns above
- `elapsed_time` — cumulative wall-clock HH:MM:SS since the run started, stamped when the row was written
- `video_pct` — how far into the current video processing had reached when the row was written
- `est_remaining` — estimated wall-clock time left for the current video, from the actual pace so far (blank for the first 1% while the estimate would be meaningless; noisy early, converges as the run progresses)

The CSV is rewritten fresh at the start of every run (each run reprocesses all videos).

---

## Project File Structure

```
D:\projects\Extractor\
├── main.py                      ← CLI entry point
├── requirements.txt
├── extractor.log                ← pipeline run log (auto-created)
├── session_log.md               ← this file
├── pipeline/
│   ├── config.py                ← all tunable parameters
│   ├── video_sampler.py         ← adaptive frame extraction (base + burst)
│   ├── detector.py              ← YOLOv8n vehicle detection gate
│   ├── ocr_engine.py            ← EasyOCR wrapper + plate regex classifier
│   ├── tracker.py               ← groups frames into per-truck events
│   ├── writer.py                ← CSV writer
│   └── utils.py                 ← image preprocessing helpers
├── videos/              ← input videos
│   └── D01_20230331*.mp4
└── output/
    ├── trucks.csv               ← main output
    └── debug/                   ← vehicle crops (optional)
```

---

## How to Run

### Easiest way — use `run.bat` (CMD or Cygwin)

A `run.bat` wrapper is provided that activates the virtual environment automatically.
No need to activate `.venv` manually every session.

```bat
cd D:\projects\Extractor

rem Process all 5 videos (recommended — run overnight)
run.bat

rem Process a single video
run.bat --input "D:\projects\Extractor\videos\D01_20230331131356.mp4"

rem With debug crops saved
run.bat --debug

rem Faster but less thorough (0.5fps instead of 1fps)
run.bat --fps 0.5
```

You can also double-click `run.bat` from File Explorer to process all videos.

### Manual way (if you prefer activating the venv yourself)

You must activate the virtual environment first, otherwise you'll get `ModuleNotFoundError: No module named 'cv2'`.

**CMD:**
```bat
cd D:\projects\Extractor
.venv\Scripts\activate.bat
python main.py
```

**PowerShell:**
```powershell
cd D:\projects\Extractor
.venv\Scripts\Activate.ps1
python main.py
```

**Cygwin:**
```bash
cd /cygdrive/d/projects/Extractor
source .venv/Scripts/activate
python main.py
```

The virtual environment stays active for the current terminal session only. You need to activate it again each time you open a new terminal.

---

## Performance

| Stage | Time/frame |
|---|---|
| YOLO detection (truck/bus only, 640px) | ~0.7s |
| EasyOCR (burst frames only, 25% scale) | ~2s (when called) |
| **Average across all frames** | **~0.9s** |
| **Estimated per 30-min video** | **~25–30 min** |
| **All 5 videos total** | **~2.5 hours** |

---

## Key Architecture Decisions

### OCR Library: EasyOCR (not PaddleOCR)
PaddleOCR has no Python 3.14 wheel. EasyOCR installed cleanly and works well.
If PaddleOCR ever supports Python 3.14, switch by setting `ocr_backend = "paddleocr"` in `pipeline/config.py`.

### Full-frame OCR (not vehicle-crop OCR)
Early testing showed the YOLO vehicle bounding box often missed the text-bearing parts of trucks (sides, rear). Running EasyOCR on the full frame (downscaled to 25%) and then associating text to the nearest vehicle bounding box gives much better coverage.

### Burst-only OCR
Running EasyOCR on every frame was too slow (~6s/frame). Now OCR only runs during motion-triggered burst mode (when a truck is actively entering the frame). Base-rate frames (empty road) only run the fast YOLO gate (~0.07s).

### OCR cooldown (2 seconds per screen region)
Even during burst, the same truck position is only OCR'd once every 2 seconds. Avoids redundant identical scans of a slow-moving truck.

### Trucks/buses only (YOLO classes 5+7)
The road has many cars and motorcycles that YOLO was detecting. Filtering to buses and trucks only reduced detected vehicles per frame from 4–5 to 0–2, cutting OCR calls significantly.

### Incremental CSV writing
Each truck event is written to the CSV the moment it closes (truck leaves frame + 8 second gap). No waiting for video or all videos to finish. The CSV file is live and can be opened in Excel while processing.

### Plate identification via regex
A dedicated YOLOv8 license plate detection model (`keremberke/yolov8n-license-plate-detection`) was attempted but requires HuggingFace authentication. Instead, EasyOCR detects all text and regex patterns classify which strings look like license plates (Indian format: `RJ 14 CA 1234`, plus UK, Malaysian, Singaporean patterns).

---

## Observed Video Content

- Camera: Indian road (license plates start with `RJ` = Rajasthan state)
- Trucks carry text: state plate, company name (e.g. "JAIPUR PVT LTD"), fleet IDs, mobile numbers
- Camera OSD overlay: "IPC" brand watermark + timestamp (filtered out by OSD regex)
- Traffic density: high — motion burst triggers almost continuously

---

## Configuration Parameters (`pipeline/config.py`)

| Parameter | Default | Description |
|---|---|---|
| `sample_fps` | 1.0 | Base frame sampling rate |
| `burst_fps` | 2.0 | Sampling rate during motion burst |
| `burst_duration_sec` | 3.0 | How long burst mode lasts after motion detected |
| `motion_threshold` | 0.015 | Pixel change fraction to trigger burst |
| `yolo_vehicle_conf` | 0.1 | YOLO confidence — big trucks near the camera score as low as 0.1 |
| `min_text_confidence` | 0.4 | EasyOCR confidence threshold (0.25 for digit-heavy reads like phone numbers) |
| `blur_variance_threshold` | 50.0 | Skip OCR if frame is too blurry |
| `plate_match_distance` | 3 | Levenshtein tolerance for same-plate matching |
| `max_gap_seconds` | 4.0 | Seconds before a truck event closes (8s chained consecutive trucks into one event) |
| `ocr_backend` | "easyocr" | OCR engine ("easyocr", "paddleocr", "auto") |

---

## Dependencies

```
numpy, pillow, opencv-python-headless
ultralytics          (YOLOv8)
easyocr              (OCR engine)
python-Levenshtein   (fast string distance)
tqdm, rich           (progress display)
huggingface-hub      (model downloads)
```

Install:
```powershell
cd D:\projects\Extractor
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

---

## Run History

| Date | Videos Processed | Trucks Found | Notes |
|---|---|---|---|
| 2026-06-09 | In progress (D01_20230331124308.mp4) | — | First full run with CSV output |

*(Update this table after each run)*

---

## Session 2026-06-10 — Data-quality fixes (verified on 00:35–00:60 of video 1)

User-reported issues: no "truck" in vehicle_type, phone_number always empty,
license_plate in only 1/26 rows, GLOBAL PETRO tanker at 00:41 missing entirely.
Root causes found and fixed:

1. **Vehicle gate missed big trucks** — YOLO scores trucks filling the frame at
   0.1–0.35 confidence; old 0.4 threshold rejected them. Tankers also classify as
   COCO class 6 "train". Fix: `yolo_vehicle_conf` 0.4 → 0.1, vehicle classes now
   5/6/7, plus cross-class NMS in `detector.py` (same truck appeared as
   truck+train+bus boxes → duplicate tracker events).
2. **Body OCR resolution destroyed text** — vehicle crops were OCR'd at 50% scale
   capped 800px; "GLOBAL PETRO" + its phone number garbled at that size but read
   perfectly at 1280px. Fix: full-res crop capped at 1280px (`ocr_engine.py`).
3. **Plate detector imgsz 640 → 1280** — at 640 it found almost no plates on the
   2592px frames.
4. **Phone numbers were deleted as noise** — `deduplicate()` dropped all pure-digit
   strings; now digit strings containing a valid 10-digit mobile survive. Phone
   extraction also fixes OCR letter-digit confusions (981i008120 → 9811008120)
   and strips +91/0/separators.
5. **Plate-crop OCR now runs every burst frame** (small crops = cheap), with the
   expensive body OCR still cooldown-gated (1.5s per 300px grid cell). Non-plate
   text found in plate crops (phones, company names near the plate) is kept as
   body text instead of being discarded.
6. **vehicle_type fallback from YOLO class** — TRUCK/BUS now always filled via
   `class_votes` on the tracker event when no type is painted on the vehicle.
7. **"TRANSPORT" misclassified as a plate** (starts with TR = Tripura state code) —
   added a word blacklist to `_looks_like_plate`.
8. **OSD clock fragments** ("12.44303", lone "PC") leaked into rows — added OSD
   regex patterns.
9. **Writer always starts a fresh CSV** — appending across restarts duplicated rows.

Verified result on the 00:35–00:60 window: GLOBAL PETRO tanker captured with
phone `9811008120`; RR ROADWAYS truck with phone `9833727904`, company name, city
Jaipur; every row has vehicle_type. Note: heavier OCR + lower detection threshold
means processing is slower than the previous configuration (quality was prioritized
over speed per user feedback).

### Round 2 (same day, after first live run still missed phones)

The live run's burst sampling (0.48s grid) plus the flat 1.5s OCR cooldown straddled
the moments when truck-side text is legible (text panels read for only ~0.4s as the
truck passes broadside). Fixes, each verified by simulating the exact live sampling
grid on the tanker window:

10. **Area-based OCR cooldown** (`main.py`) — vehicles covering ≥8% of the frame get
    body OCR every 0.85s (every other burst sample); small/far ones stay at 1.5s.
11. **Burst interval 12 → 11 native frames** (`burst_fps` 2.0 → 2.2) — odd interval
    de-aliases the sampling grid against legibility windows.
12. **Plate-crop digit reads floored at 0.12** confidence (phones near plates often
    OCR at ~0.17).
13. **Email extraction into `website` column** — handles OCR-mangled forms like
    "EMAILINFO @RRROADWAYSPVILLDCOM" → `info@rrroadwayspvilldcom`.
14. **Plate blacklist matches substrings** — truncated reads like "TRANSPOR" no
    longer become license plates.
15. **Company names reject strings with 3+ consecutive digits** (plate/fleet reads
    like "FRJASCR2455").

### Round 3 (2026-06-11) — Speed: 3.2x faster, same data quality

User reported ~8s/frame (12h ETA per video). Measured fixes, verified by re-running
the first 110s of video 2 (11:12 vs 35:21 wall time, identical plates/phones):

16. **Two-pass OCR** (`ocr_engine.py read_text_two_pass`) — EasyOCR's text-DETECTION
    network was the bottleneck (~7s on a 1280px crop). Now detection runs on a 640px
    downscale and only recognition runs at full res: 7.7s → 2.35s per body scan,
    with equal-or-better reads.
17. **Plate detector gated to body-OCR frames** (was every burst frame).
18. **Vehicles <2% of frame skip body OCR** — text never legible at that size.
19. **Sampler uses `cap.grab()` for skipped frames** — avoids full decode of the
    ~80% of native frames that are never processed.
20. **Phone variants collapse** (Levenshtein ≤2, most-frequent read wins).
21. **Noise rows dropped** — events with no plate, no text, <5 frames (false
    detections at the 0.1 conf gate) are no longer written. Row count for the same
    footage: 44 → 22, all data rows retained.

Realistic pace after round 3: ~2–2.5h per 30-min video (~11–12h for all 5; run
overnight). Further speedup needs parallel video workers (`--workers 2`,
implemented in round 5) or a CUDA GPU.

### Round 4 (2026-06-11) — CSV semantics

22. **`truck_id` is now a plain sequential id** (`T0001`, `T0002`, ...) — it
    previously duplicated `license_plate` when a plate was found, which was
    confusing and redundant.
23. **Plates must contain at least one digit** — letters-only reads like "ASHOK"
    (the Ashok Leyland manufacturer logo, which starts with Assam's state code AS)
    were being classified as plates. Letters-only strings now flow to the text
    columns instead; manufacturer names (ASHOK, LEYLAND, MAHINDRA, APOLLO) added
    to the plate blacklist.

### Round 5 (2026-06-12) — Parallel video workers

24. **`--workers N` is now implemented** (`run.bat --workers 2`). Each worker
    process handles one whole video with its own copies of the models; closed
    truck events are shipped back to the parent over a queue, so there is still
    exactly one `trucks.csv`, written incrementally with sequential `truck_id`s
    (rows from different videos interleave by close time — sort/filter by
    `video_file`). Per-worker CPU threads are split evenly (torch + OpenCV) to
    avoid thrashing. Per-video processing logic is untouched, so output quality
    is identical to a sequential run.
    - On this machine (4 cores / 12 GB) use `--workers 2`, no higher: each
      worker needs ~2 GB RAM, and beyond 2 the cores are oversubscribed.
    - Expected effect: per-video time roughly unchanged-to-slower, but two
      videos run at once → all 5 videos in roughly 8–9h instead of ~13–15h.
    - In parallel mode the tqdm bar is replaced by a log line every 200 frames
      per video (two live bars from two processes would garble the console).
25. **`est_remaining` clarified (not a bug)**: it is recomputed each row from
    average pace so far (`elapsed × remaining_frac / done_frac`). Below ~5% it
    is noisy and typically climbs as the early underestimate corrects; it
    behaves like a countdown once pace stabilizes.
26. **`vehicle_type` values like GOODS CARRIER / SCHOOL BUS come from text
    painted on the vehicle** (text wins over the YOLO class). GOODS CARRIER *is*
    a truck — Indian goods vehicles are required to display it. `BUS` appears
    only when no type text was read and the detector's majority class was bus —
    which can be a real bus *or* a truck (big trucks often score as "bus"/"train"
    in YOLO; the GLOBAL PETRO tanker was detected as "train"). Pre-skipping by
    YOLO class would silently drop real trucks, so non-TRUCK rows are kept;
    filter them in Excel if needed.

### Round 6 (2026-06-12) — Corrupt-frame recovery (first full-video run audit)

First completed run (video 2, `D01_20230331131356.mp4`): 339 trucks in 146.3 min.
Audit found the run silently stopped at ts=1456s of 1820s — the last ~6 minutes
of footage were never processed.

27. **Root cause: corrupt H.264 frames inside the DVR file** (frame 36401 @
    1456.0s, plus 1743.8s and 1804.8s). The sampler treated any `grab()`/`read()`
    failure as end-of-video and broke out. Verified by walking the file
    sequentially: the failures are reproducible, and seeking one frame past the
    bad frame resumes decoding fine.
28. **Sampler now recovers**: on a decode failure before the end of the video it
    logs a warning (`corrupt frame N — skipping past it`), seeks past the bad
    frame, and continues; it gives up only after 200 failures in one video.
    Failures within the last second of the file are treated as the (normal)
    unreadable DVR tail. Verified: sampler now reaches ts=1819.8s on video 2.
29. **End-of-video rows report true coverage** — the finalize path used to stamp
    `video_pct=100.0%` unconditionally (which masked the early stop); it now
    writes the actual fraction covered, the completion banner logs
    `covered HH:MM:SS of HH:MM:SS`, and a warning fires if coverage < 98%.
30. **tqdm frame estimate factor 1.5 → 2.2** (measured burst overhead on this
    dense footage), so the progress bar no longer overflows its total.
31. `sample_frames()` gained a `start_sec` parameter (debugging aid — lets a
    test start sampling mid-video).

Video-2 full-run data quality (339 rows): 90 plates (25 HIGH), 218 company
names, 45 phone numbers, 2 websites; vehicle_type: 302 TRUCK / 27 BUS /
5 GOODS CARRIER / 5 SCHOOL BUS. Backup: `output/trucks_video2_full_20260612.csv`.
True pace: 146 min for 24.3 min of footage ≈ ~3h for a full 30-min video
(sequential). Note: `extractor.log` already captures everything except the tqdm
bar — no need to hand-copy console output into run_stdout.txt.

---

## Issues / Known Limitations

1. **Plate OCR accuracy** — EasyOCR sometimes misreads characters (0↔O, 1↔I). The Levenshtein-based tracker groups similar readings, so the final plate in the CSV is the most-consistent reading across multiple frames.

2. **Plate regex coverage** — Regex patterns cover Indian, UK, Malaysian, Singaporean plates. Other formats may not be classified as plates and appear in `body_text` instead. Add patterns to `_PLATE_PATTERNS` in `pipeline/ocr_engine.py` if needed.

3. **Python 3.14 limitation** — PaddleOCR (higher accuracy than EasyOCR) has no Python 3.14 wheel. Install Python 3.12 alongside if better accuracy is needed.

4. **No GPU** — Machine has Intel Iris Xe (no CUDA). With an NVIDIA GPU, EasyOCR would be ~5–10× faster (minutes instead of hours per video).

5. **Side-mounted camera** — many trucks pass with their plate never facing the camera, so `license_plate` will legitimately be empty for a large share of rows.

6. **Consecutive trucks in the same lane** can briefly overlap in the tracker (IoU matching), so occasionally a word from one truck appears in the next truck's row.

---

## Round 7 (2026-06-15) — Data platform: PostgreSQL + web app + image/stream APIs

Major expansion from "CSV per run" to a multi-source data platform feeding a
broker-facing app. End goal: store every truck sighting in a database and show
brokers a newest-first list (with photo + click-to-call) so they can phone drivers.

**Three input sources, one record shape:**
1. Video files — existing pipeline (now also writes the DB).
2. Live stream — `pipeline/stream_runner.py` (RTSP/HTTP, wall-clock timestamps,
   reconnect on dropout). Run: `run_stream.bat <url>`.
3. Still images — `POST /api/trucks/from-images`, up to **5 photos of one truck**
   → **one** consolidated row (`pipeline/image_api.py` feeds all photos into a
   single TruckEvent, bypassing tracker IoU).

**Key refactor — shared extraction:** all the semantic logic (company/phone/
website/vehicle_type/city/other + dedup + noise gate) moved from `writer.py` into
`pipeline/extract.py::extract_truck_fields(event) -> dict|None`. CSV, DB, and the
image API all call it, so extraction is identical everywhere. `writer.py` is now a
thin CSV adapter (output byte-identical; `is_noise_event`/`deduplicate`
re-exported for back-compat).

**Database (`pipeline/db.py`):** PostgreSQL 16, SQLAlchemy 2.0 + psycopg3, one
`trucks` table. Absolute `detected_at` (indexed DESC) drives newest-first — for
video it's `recording_dt + offset` where `recording_dt` is parsed from the DVR
filename (`pipeline/timestamps.py`, e.g. D01_20230331124308 → 2023-03-31 12:43:08);
image/stream use `now()`. JSONB columns hold `plate_candidates` + full `body_texts`.
`source` enum = video/image_api/stream. `image_path` points at a representative
photo crop.

**DB sink (`pipeline/db_writer.py`):** `TruckDBWriter` has the same duck-typed
`.write(event, progress)->bool` as the CSV writer, so `process_video` is unchanged.
Postgres handles concurrency → parallel video workers each write directly (no parent
queue for DB). `main.py` gains `--sink {csv,db,both}` (default `both`).

**Representative photo:** `TruckEvent` gained `best_crop`/`best_crop_score`;
`process_video(..., capture_crops=True)` keeps the largest×sharpest body crop per
truck (must `.copy()` — the crop is a view into the full frame); `TruckDBWriter`
saves it to `output/crops/<id>.jpg`. Off for plain CSV runs.

**Web app (`webapp/app.py`, FastAPI + Jinja2 + Tailwind CDN):**
- `GET /` broker UI — newest-first cards, search (plate/company/phone/city),
  pagination, `tel:` click-to-call, photo.
- `GET /api/trucks`, `GET /api/trucks/{id}` — JSON.
- `POST /api/trucks/from-images` — image ingestion (models lazy-loaded on first hit;
  blur gate relaxed so soft phone photos still OCR).
Run: `run_webapp.bat` → http://localhost:8000.

**Deployment (Docker, for later VM rollout):** `docker-compose.yml` (postgres +
web), `Dockerfile` (python:3.12-slim), `.env.example`. Pipeline runs on the host
(needs videos + CPU); DB+app containerized. Local-without-Docker fallback: portable
Postgres via `start_db.bat`/`stop_db.bat` (binaries in `pgsql/`, data in `pgdata/`).
Setup + commands documented in `DEPLOY.md`. Verify with `scripts/verify_db.py`.

**New deps:** sqlalchemy>=2, psycopg[binary], fastapi, uvicorn[standard],
python-multipart, pydantic-settings (all confirmed to import on Python 3.14).

---

## Round 8 (2026-06-15) — Mobile field-report API (replaces still-image endpoint)

The still-image API was redefined for an already-built Android/iOS app: on-road users
capture 1-5 photos of a truck, fill a form, and submit → one DB row. **Images are
processed (OCR) but never stored.**

**Endpoint:** `POST /api/trucks/report` (multipart/form-data), replacing the old
`/api/trucks/from-images`. Fields: `images` (1-5, required), `phone_number` (required),
`vehicle_number`, `loaded_status`, `number_of_wheels`, `location`, `latitude`,
`longitude`, `captured_at` (ISO), `reported_by`. (We define the contract; app dev matches.)

**Reconciliation (`pipeline/reports.py::reconcile`, raises `ReportRejected`→422):**
- Vehicle number: OCR the plate; if a plate is read AND it disagrees with the user's
  typed number (normalized, Levenshtein > `config.plate_match_distance`) → **reject**.
  Else `license_plate` = the clean reported value; `plate_confidence` ∈
  VERIFIED / REPORTED / OCR_ONLY / NONE.
- Phone (mandatory): **merge** user-typed + OCR-read (collapsing near-dupes). Columns
  `phone_reported`, `phone_ocr` keep provenance; `phone_number` = merged. `phone_status`
  ∈ MATCH / MERGED / REPORTED_ONLY.
- Company/vehicle_type/city/other_text taken from OCR; loaded/location/lat-long/wheels/
  reported_by/captured_at from the report.

**New DB columns** (added by `scripts/migrate_report_fields.py` — idempotent
`ALTER TABLE … ADD COLUMN IF NOT EXISTS`, since `create_all` can't alter an existing
table): `loaded_status, location, latitude, longitude, num_wheels, phone_reported,
phone_ocr, reported_by`. `image_path` stays NULL for reports.

**Other changes:** `pipeline/image_api.py` got `capture_crop=False` (reports skip the
photo crop); `pipeline/db_writer.py` got `insert_report(fields)` (full-row insert, no
crop, no extract); broker UI shows loaded/wheels/location badges and relabels
`image_api` → "Field report".

**Verified (2026-06-15):** reconcile unit tests (VERIFIED/REJECT/REPORTED/MERGED/
OCR_ONLY) + live endpoint (happy path 200 with all fields, `image_path=NULL`, **0 crop
files written**, missing-phone→422, 6-images→400). Test row cleaned up afterward.

### Round 8b — Trust model for paid contributors (no blind trust)

Reframe: mobile contributors are anonymous and **paid for correct uploads**, so a
report is never trusted on the user's word — the photos are the only proof.

- **No more 422 on plate mismatch.** Every report is **stored**, but with a
  `verification_status`: **VERIFIED** only when the photos confirm the typed vehicle
  number (fuzzy match); otherwise **UNVERIFIED** (no plate read / no number typed /
  conflict) with a human-readable `reason`. UNVERIFIED rows are shown to brokers with
  an "⚠ Unverified" badge and are not auto-trusted/paid.
- **Abuse tracking:** new `submission_log` table — one row per `/report` call
  (`reported_by`, `status`, `reason`, `vehicle_reported`, `vehicle_ocr`,
  `phone_reported`, `phone_ocr`, `images_count`, `truck_id`). `insert_report` writes
  the truck row + the audit row in one transaction. Lets you spot contributors who
  repeatedly submit unverifiable junk.
- Schema: `verification_status` column added to `trucks` (+ index);
  `scripts/migrate_report_fields.py` adds it and `init_db()` creates `submission_log`.
- `pipeline/reports.py::reconcile` no longer raises; returns
  `verification_status` + `reason` + `vehicle_reported`/`vehicle_ocr`. UI adds the
  verified/unverified badge. Verified end-to-end (verdicts, endpoint, audit row, UI).

Abuse query (DBeaver): contributors by unverified ratio —
`SELECT reported_by, count(*) total, count(*) FILTER (WHERE status='UNVERIFIED') unverified FROM submission_log GROUP BY reported_by ORDER BY unverified DESC;`

### Round 8c — Telecaller review workflow (reward approval)

Human approval layer on top of the machine verification. Two independent statuses:
`verification_status` (machine: VERIFIED/UNVERIFIED) and **`review_status`** (telecaller:
PENDING→PASSED/REJECTED). **PASSED = contributor is reward-eligible.**

- **Schema:** `trucks` gains `review_status` (indexed, default PENDING for reports via
  `insert_report`), `reviewed_by`, `reviewed_at`, `review_note` (migrate script extended).
- **PATCH `/api/trucks/{id}`** (admin auth): body `{review_status, review_note?, reviewed_by?}`;
  sets status (upper-cased), reviewer=admin identity, reviewed_at=now; 200/400/404/401.
- **Admin auth (simple shared password):** env `ADMIN_PASSWORD` (+ `ADMIN_USER`, default
  admin; local-dev default password 'admin' with a warning). `_check_admin` accepts an
  `admin_token` cookie (browser) or `X-Admin-Token` header (API), constant-time compare.
  `require_admin` dependency → 401. Routes `/review/login` (GET form, POST sets cookie),
  `/review/logout`.
- **`/review` page** (auth, `templates/review.html`): field-report queue, filters
  review(pending default/passed/rejected/all) + verification(all/verified/unverified),
  click-to-call phone, GPS map link, **Pass/Reject buttons** → JS `fetch` PATCH (cookie),
  reload. Broker `index` cards gained a "✓ Passed" badge.
- Submit response + `GET /api/trucks/{id}` now include `review_status` (PENDING on submit)
  so the contributor app can poll for approval.
- **Reward query:** `SELECT * FROM trucks WHERE source='image_api' AND review_status='PASSED'`.
- Verified end-to-end: PENDING on insert, 401 without token, PATCH→PASSED with reviewer,
  400 bad status, 404 unknown id, login redirect+cookie, queue lists/filters + Pass moves
  rows between filters. Test rows cleaned up.

### Round 8d — No photo storage anywhere (policy)

User reaffirmed: never store images on the system — process them, keep only extracted
data. The mobile report API already stored nothing; the *video* pipeline had been saving
representative thumbnails to `output/crops/` for the broker app. Removed entirely:
- `main.py`: all `capture_crops` call sites → `False`, `crops_dir=None` (sequential +
  parallel workers). Video `--sink db` runs now write no image files.
- DB: `UPDATE trucks SET image_path=NULL` (41 rows); `output/crops/` deleted (~90 MB).
- `webapp/app.py`: dropped the `/crops` StaticFiles mount + `_CROPS_DIR`; `index.html`
  cards no longer render a photo area (broker app shows no photos by design).
- `docker-compose.yml`: `web` service no longer mounts `./output` (app reads only the DB).
- `TruckEvent.best_crop` / `db_writer._save_crop` remain but are dormant (never invoked).
Verified: video writer with a crop present + crops_dir=None writes 0 files, image_path
NULL; webapp import doesn't recreate `output/crops`; pages render without the photo block.

---

## Round 9 (2026-06-16) — Broker console UX upgrade (design review + 5 increments)

Principal-design review written to `DESIGN_REVIEW.md` (critique + full spec: layout,
component hierarchy, column triage, card/filter/slide-over, scale strategy, future-proofing).
Implemented an incremental rollout (no rewrite) on the Jinja2 broker page, per user direction:

1. **CALL as hero column** — phone is the dominant cell: bigger emerald tap target, 5+5
   grouped number, copy button (`copyPhone`), `+N` popover for extra numbers, **E.164**
   `tel:` via a `_phone_list` Jinja filter (`templates.env.filters['phones']`).
2. **Trust badge replaces plate-confidence STATUS** — `✓ Verified` (VERIFIED or review
   PASSED) / `✕ Rejected` / `⚠ Unverified` (image_api) / `Auto` (video/stream). OCR
   "extraction quality" moved to the detail panel.
3. **Freshness dot + recency filter** — green(<24h)/amber(<7d)/grey dot per row
   (`_fresh_bucket`); `fresh` filter param **defaults to All Time** (most data historical);
   `_fresh_cutoff` applies the window.
4. **Quick-filter chips + instant apply (HTMX)** — light filter bar: Source, Type (distinct
   `vehicle_type`), Location (distinct `city`), Freshness, Verified-only toggle. One
   `<form id="filters">` with `hx-get="/" hx-target/select="#results"` swaps just the
   results, no full reload; active-filter count + Clear; differentiated empty states.
   `base_qs` centralizes filter params for sort/pagination links.
5. **Row → detail slide-over (HTMX)** — `›` chevron per row `hx-get`s
   `/trucks/{id}/panel` into an off-canvas `#detail-panel` (`detail_panel.html`):
   Contact (all phones call/copy), Overview, Verification/review, collapsible Raw.

Explicitly deferred per user: **unique-truck collapse** (keep raw sightings) and any
**image storage** (none). HTMX added via CDN; non-JS fallback preserved (form Search +
link hrefs). Verified server-side via TestClient: filter bar, HTMX attrs, type/verified
filtering, base_qs links, panel fragment (200 + 404). Graduation path (React + TanStack,
keyset pagination, tsvector search) documented in `DESIGN_REVIEW.md`.
