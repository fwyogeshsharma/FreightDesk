# Field-Report API — Contract for the Mobile App

Submit one truck sighting (1–5 photos + details) → creates one record.

- **Method / URL:** `POST /api/trucks/report`
- **Base URL:** `http://34.31.185.19:8090` (HTTPS coming — only the base changes; local dev: `http://localhost:8000`)
- **Content-Type:** `multipart/form-data`
- **Auth:** optional. If the user is logged in, send `Authorization: Bearer <token>` and
  the report is attributed to that account; without it the submission is anonymous
  (still accepted). See **Authentication** below.
- **Important — this is ASYNCHRONOUS.** The endpoint validates + accepts the report and
  returns **`202 Accepted` immediately** with a record `id`; OCR runs in the background. The
  app must then **poll `GET /api/trucks/{id}`** until `processing_status` is `DONE` (or
  `FAILED`) to get the OCR result. See **Polling for the result** below. *(This replaced the
  old behavior where the POST blocked until OCR finished and returned the full result.)*
- Photos are kept in temporary storage for up to ~2 days (for OCR + telecaller review), then
  auto-deleted. Only the extracted data is permanent.

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

## Success response — `202 Accepted`

The report is **accepted immediately** — OCR has *not* run yet, so the body is small:

```json
{
  "id": 124,
  "processing_status": "QUEUED",
  "review_status": "PENDING",
  "images_accepted": 3,
  "status_url": "/api/trucks/124",
  "message": "Report accepted; OCR is running in the background."
}
```

| Field | Meaning |
|---|---|
| `id` | The stored record id. Poll `GET /api/trucks/{id}` (= `status_url`) for the result. |
| `processing_status` | `QUEUED` right after submit → becomes `PROCESSING` → `DONE` (or `FAILED`). |
| `review_status` | Always `PENDING` right after submit (a telecaller reviews it once processed). |
| `images_accepted` | How many uploaded images were decodable and stored. |
| `status_url` | Convenience path to poll for the result. |

---

## Polling for the result

After `202`, poll **`GET /api/trucks/{id}`** every few seconds. It returns the full `truck`
record; watch **`processing_status`**:

| `processing_status` | Meaning | App should… |
|---|---|---|
| `QUEUED` / `PROCESSING` | OCR not finished yet | show "Processing…", poll again in ~3 s |
| `DONE` | OCR finished — read `verification_status`, `license_plate`, `company_name`, … | stop processing-poll; show result |
| `FAILED` | Photos unreadable/expired or an error (`processing_error`) | ask the user to resubmit |

> On a small server OCR can take a while (seconds to a minute under load). Suggested client:
> poll every ~3 s, and after ~60 s show "still processing — we'll update it shortly" rather
> than blocking the user. The record is safely stored the whole time.

Once `processing_status == "DONE"`, the same record carries the OCR result (full shape is the
`GET /api/trucks/{id}` object):

```json
{
  "id": 124,
  "processing_status": "DONE",
  "processed_at": "2026-06-16T14:31:10.001000+05:30",
  "verification_status": "VERIFIED",
  "review_status": "PENDING",
  "license_plate": "RJ14CA1234",
  "company_name": "SHREE BALAJI TRANSPORT",
  "phone_number": "9811008120",
  "vehicle_type": "TRUCK",
  "city": "Jaipur",
  "body_texts": ["GOODS CARRIER", "SHREE BALAJI TRANSPORT", "9811008120"],
  "reported_by_user_id": 42,
  "...": "(plus all the other truck fields)"
}
```

| Field on the DONE record | Meaning |
|---|---|
| `verification_status` | `VERIFIED` if the photos confirmed the typed `vehicle_number`; else `UNVERIFIED`. |
| `review_status` | `PENDING` until a telecaller decides; **`PASSED` = reward-eligible**; `REJECTED` = not. |
| `processing_error` | Why it `FAILED` (only set on failure). |

> A report can be `DONE` + `UNVERIFIED` + `PENDING` all at once — those are three independent
> things (did OCR finish · did photos confirm the plate · has a human approved it). A truck is
> only **reward-eligible after** a telecaller sets `review_status` to `PASSED`.

---

## Error responses

Validation still happens **synchronously** on the POST (before the `202`), so these return right away:

| Status | When | Body |
|---|---|---|
| `400 Bad Request` | `phone_number` blank, more than 5 images, or no image was decodable | `{ "detail": "phone_number is required" }` (message varies) |
| `422 Unprocessable Entity` | A required field is missing entirely (`images` or `phone_number` not sent) | `{ "detail": [ { "loc": ["body","phone_number"], "msg": "field required", ... } ] }` |

A photo that fails OCR *after* acceptance does **not** error the request — the record is stored
and ends up `processing_status: FAILED` (if no image was usable) or `DONE` + `UNVERIFIED`.

---

## The two polls (don't confuse them)

The app polls the **same** `GET /api/trucks/{id}` for two different milestones:

1. **Processing** (seconds): `processing_status` `QUEUED→PROCESSING→DONE`. Poll tightly right
   after submit; stop once `DONE`/`FAILED`.
2. **Reward review** (minutes–hours, human): after `DONE`, a telecaller calls the number and
   sets `review_status` to `PASSED` (reward-eligible) or `REJECTED`. Poll occasionally — or just
   use **`GET /api/auth/me/reports`** (bearer), which lists the user's reports + a
   `{pending, passed, rejected}` summary for the reward screen.

```bash
curl http://localhost:8000/api/trucks/124        # read processing_status, then review_status
```

---

## Tips for the mobile dev
- Send each photo as a separate `images` part (don't zip or base64 them).
- Treat `202` as "received & queued" — immediately move the user off the upload screen; don't
  block on a response. Then poll `status_url` for `processing_status`.
- Prefer logging the user in and sending `Authorization: Bearer <token>` so rewards attribute
  to the right account (replaces the old free-text `reported_by`).
- Interactive, always-up-to-date API docs are served at **`/docs`** (Swagger UI) and
  **`/redoc`** — e.g. `http://localhost:8000/docs`.
