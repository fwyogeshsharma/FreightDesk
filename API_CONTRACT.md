# Field-Report API — Contract for the Mobile App

Submit one truck sighting (1–5 photos + details) → creates one record.

- **Method / URL:** `POST /api/trucks/report`
- **Base URL:** `http://34.31.185.19:8090` (HTTPS coming — only the base changes; local dev: `http://localhost:8000`)
- **Content-Type:** `multipart/form-data`
- **Auth:** optional. If the user is logged in, send `Authorization: Bearer <token>` and
  the report is attributed to that account; without it the submission is anonymous
  (still accepted). See **Authentication** below.
- **Important:** the photos are processed (OCR) and then **discarded** — images are
  never stored. We keep only the extracted data.

---

## Authentication (recommended)

Contributors register and log in from the app. Auth is a **bearer token** — store it
after register/login and send it as `Authorization: Bearer <token>` on report submissions
so rewards attribute to the right person. Tokens are long-lived (30 days); on `401` from
`/api/auth/me`, send the user back through login.

| Method / URL | Body (JSON) | Returns |
|---|---|---|
| `POST /api/auth/register` | `{ "phone", "password", "display_name"?, "email"? }` | `201` `{ "token", "user" }` |
| `POST /api/auth/login` | `{ "phone", "password" }` | `200` `{ "token", "user" }` |
| `GET  /api/auth/me` | — (bearer) | `200` `{ "user" }` |
| `GET  /api/auth/me/reports?limit=&offset=` | — (bearer) | `200` `{ total, summary, reports[] }` |
| `POST /api/auth/logout` | — (bearer) | `200` `{ "ok": true }` |

**`GET /api/auth/me/reports`** returns the logged-in user's own submissions (newest first)
plus a `summary` of `{pending, passed, rejected}` counts for the reward UI. `passed` =
reward-eligible. Each item is a full truck record (same shape as the report response's
`truck`). Only reports submitted **while logged in** appear (anonymous ones aren't linked).

- `phone` is the identity (normalized — spaces/dashes ignored); `password` min 6 chars.
- Registration always creates a **contributor**. (Telecaller/admin accounts are internal,
  created by an admin — not via this API.)
- Errors: `409` phone already registered, `400` invalid input, `401` bad credentials.

```bash
# register, then submit an attributed report
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"phone":"9811008120","password":"secret123","display_name":"Ravi"}' | jq -r .token)

curl -X POST http://localhost:8000/api/trucks/report \
  -H "Authorization: Bearer $TOKEN" \
  -F "images=@truck_front.jpg" -F "phone_number=9928001122"
```

`user` object shape: `{ id, phone, username, email, display_name, role, is_active, registration_source, created_at }` (never includes the password). For mobile contributors `username` is always `null` — they're identified by `phone`.

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
| `reported_by` | string | optional | Free-text reporter label for **anonymous** submissions. When a `Bearer` token is sent this is ignored — the logged-in account is used instead (preferred). |

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
    "reported_by": "Ravi",
    "reported_by_user_id": 42,
    "reporter_phone": "9811008120",
    "verification_status": "VERIFIED",
    "review_status": "PENDING",
    "reviewed_by": null,
    "reviewed_by_user_id": null,
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
  "phone_status": "MATCH",
  "reported_by_user_id": 42
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
| `reported_by_user_id` | The logged-in contributor's id when a bearer token was sent; `null` for anonymous submissions. |

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
- Prefer logging the user in and sending `Authorization: Bearer <token>` so rewards
  attribute to the right account (replaces the old free-text `reported_by`).
- Treat `200` as "received & stored"; show the user "Pending review" until a later
  `GET` shows `review_status: PASSED`.
- Interactive, always-up-to-date API docs are served at **`/docs`** (Swagger UI) and
  **`/redoc`** — e.g. `http://localhost:8000/docs`.
