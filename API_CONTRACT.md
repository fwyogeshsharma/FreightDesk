# Field-Report API — Contract for the Mobile App

Submit one truck sighting (1–5 photos + details) → creates one record.

- **Method / URL:** `POST /api/trucks/report`
- **Base URL:** `http://<server-host>:8000` (dev: `http://localhost:8000`)
- **Content-Type:** `multipart/form-data`
- **Auth:** none required to submit.
- **Important:** the photos are processed (OCR) and then **discarded** — images are
  never stored. We keep only the extracted data.

---

## Request fields (multipart form)

| Field | Type | Required | Notes |
|---|---|---|---|
| `images` | file × **1–5** | ✅ yes | Repeat the part once per photo (same field name `images`). Max 5. JPEG/PNG. |
| `phone_number` | string | ✅ yes | Phone the user read on the truck (10-digit Indian mobile). |
| `vehicle_number` | string | optional | Number plate the user read, e.g. `RJ14CA1234`. |
| `loaded_status` | string | optional | `loaded` or `unloaded`. |
| `number_of_wheels` | integer | optional | e.g. `12`. |
| `location` | string | optional | Address / place text, e.g. `NH-48, Jaipur`. |
| `latitude` | float | optional | GPS, e.g. `26.9124`. |
| `longitude` | float | optional | GPS, e.g. `75.7873`. |
| `captured_at` | string (ISO-8601) | optional | When the photo was taken, e.g. `2026-06-16T14:30:00+05:30`. Defaults to server time. |
| `reported_by` | string | optional | The app user's id/username — **send this** so rewards can be attributed. |

### Example (curl)
```bash
curl -X POST http://localhost:8000/api/trucks/report \
  -F "images=@truck_front.jpg" \
  -F "images=@truck_side.jpg" \
  -F "phone_number=9811008120" \
  -F "vehicle_number=RJ14CA1234" \
  -F "loaded_status=loaded" \
  -F "number_of_wheels=12" \
  -F "location=NH-48, Jaipur" \
  -F "latitude=26.9124" \
  -F "longitude=75.7873" \
  -F "captured_at=2026-06-16T14:30:00+05:30" \
  -F "reported_by=driver_app_9087"
```

---

## Success response — `200 OK`

```json
{
  "truck": {
    "id": 124,
    "detected_at": "2026-06-16T14:31:07.512000+05:30",
    "source": "image_api",
    "source_ref": "driver_app_9087",
    "license_plate": "RJ14CA1234",
    "plate_confidence": "VERIFIED",
    "company_name": "SHREE BALAJI TRANSPORT",
    "phone_number": "9811008120",
    "website": null,
    "vehicle_type": "TRUCK",
    "city": "Jaipur",
    "other_text": "GOODS CARRIER",
    "frames": 3,
    "first_seen_sec": 0.0,
    "last_seen_sec": 2.0,
    "loaded_status": "LOADED",
    "location": "NH-48, Jaipur",
    "latitude": 26.9124,
    "longitude": 75.7873,
    "num_wheels": 12,
    "phone_reported": "9811008120",
    "phone_ocr": "9811008120",
    "reported_by": "driver_app_9087",
    "verification_status": "VERIFIED",
    "review_status": "PENDING",
    "reviewed_by": null,
    "reviewed_at": null,
    "review_note": null,
    "plate_candidates": { "RJ14CA1234": 3 },
    "body_texts": ["GOODS CARRIER", "SHREE BALAJI TRANSPORT", "9811008120"],
    "image_path": null,
    "created_at": "2026-06-16T14:31:07.611000+05:30"
  },
  "images_processed": 3,
  "verification_status": "VERIFIED",
  "review_status": "PENDING",
  "reason": "vehicle number confirmed from photos",
  "plate_status": "VERIFIED",
  "phone_status": "MATCH"
}
```

### Top-level fields the app should read
| Field | Meaning |
|---|---|
| `truck.id` | The stored record id. Use it to poll status later (see below). |
| `verification_status` | `VERIFIED` if the photos confirmed the typed vehicle number; else `UNVERIFIED`. |
| `review_status` | Always `PENDING` right after submit (a telecaller reviews it next). |
| `reason` | Human-readable explanation of the verification result. |
| `phone_status` | `MATCH` / `MERGED` / `REPORTED_ONLY` — how the typed phone compared to OCR. |
| `images_processed` | How many of the uploaded images were readable. |

> **Note:** Submission always succeeds with `200` even when `verification_status` is
> `UNVERIFIED` — the record is still stored and a telecaller will review it. A truck
> is only **reward-eligible after** a telecaller sets `review_status` to `PASSED`.

### UNVERIFIED example (photos couldn't confirm the plate)
```json
{
  "truck": { "id": 125, "verification_status": "UNVERIFIED", "review_status": "PENDING", "...": "..." },
  "images_processed": 2,
  "verification_status": "UNVERIFIED",
  "review_status": "PENDING",
  "reason": "no plate readable in the photos to confirm the vehicle number",
  "plate_status": "REPORTED",
  "phone_status": "REPORTED_ONLY"
}
```

---

## Error responses

| Status | When | Body |
|---|---|---|
| `400 Bad Request` | `phone_number` blank, more than 5 images, or no image was decodable | `{ "detail": "phone_number is required" }` (message varies) |
| `422 Unprocessable Entity` | A required field is missing entirely (`images` or `phone_number` not sent) | `{ "detail": [ { "loc": ["body","phone_number"], "msg": "field required", ... } ] }` |

---

## Checking status later (reward flow)

A submission starts as `review_status: "PENDING"`. A telecaller calls the number to
confirm, then marks it `PASSED` (reward-eligible) or `REJECTED`. The app can poll:

- **`GET /api/trucks/{id}`** → returns the same `truck` object; read `review_status`
  to know when it becomes `PASSED`.

```bash
curl http://localhost:8000/api/trucks/124
```

---

## Tips for the mobile dev
- Send each photo as a separate `images` part (don't zip or base64 them).
- Always include `reported_by` so rewards attribute to the right user.
- Treat `200` as "received & stored"; show the user "Pending review" until a later
  `GET` shows `review_status: PASSED`.
- Interactive, always-up-to-date API docs are served at **`/docs`** (Swagger UI) and
  **`/redoc`** — e.g. `http://localhost:8000/docs`.
