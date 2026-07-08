"""One-time migration: import Ramdayal's team's manually-collected field-survey
spreadsheet (Google Form responses — plate/driver/owner/transporter info gathered
by physically visiting/calling trucks) into the `trucks` table.

This is NOT OCR output — a human read the plate off a photo and typed it in, and
the team already phoned these contacts (see the Remarks column: "BUSY", "NO
Receive", "INVALID NO.", etc.). Design decisions, agreed with the user:

  - source = image_api. Keeps these on the `/` broker page immediately (more
    inventory for brokers) without a schema change. They're deliberately marked
    already-reviewed (review_status=PASSED, verification_status=VERIFIED — a
    human directly reading a photo they took is treated as at least as reliable
    as OCR auto-verification) so they do NOT clutter the telecaller /review queue,
    which exists for triaging *unreviewed* paid-contributor submissions.
  - processing_status=DONE (no async OCR job — nothing to poll/process).
  - Photos: the sheet only has Google Drive links (owned by the collectors'
    personal accounts). With --with-photos, each Drive file id is extracted from
    "Truck Photo 1"/"2", downloaded anonymously (the folder must be shared
    "Anyone with the link — Viewer"), and re-hosted through the existing
    pipeline.storage backend under the same "reports/<id>/<idx>.ext" key
    convention the mobile /report upload path uses — so it's subject to the same
    ~2-day retention as any other report photo. Without --with-photos, image_keys
    is left NULL and the raw Drive links are kept in other_text instead.
  - Every field not covered by a dedicated column (driver/owner/transporter name,
    destinations+rate, axle, weight capacity, make/model, dimensions, remarks,
    collector, collection date) is folded into other_text as labeled lines —
    nothing from the sheet is silently dropped, it's just not queryable/filterable.
  - phone_number merges Driver + Motor Malik ("malik" = owner) + Transporter
    phone, cleaned and deduped, in that priority order (most-actionable contact
    first) — matches the existing "; "-joined multi-phone convention. A few rows
    had the phone typed into the *name* column instead of the phone column (e.g.
    row 45: Motor Malik Name = "9837803744", Motor Malik Phone = blank) — this is
    detected (a "name" field with no letters in it) and recovered.
  - Rows with no recoverable phone number at all are skipped (a telecaller can't
    act on a truck with no callable number — same policy as the video/stream
    writer's require_phone). Rows explicitly marked a test record in Remarks are
    skipped too.
  - Idempotent: each imported row gets `source_ref = "legacy_field_survey_row_N"`
    (N = the sheet row number). Re-running the script skips rows whose source_ref
    already exists, so it's safe to run again (e.g. once against local dev, then
    again against prod) without creating duplicates.

Usage:
    .venv\\Scripts\\python.exe scripts\\import_legacy_field_survey.py --dry-run
    .venv\\Scripts\\python.exe scripts\\import_legacy_field_survey.py
    .venv\\Scripts\\python.exe scripts\\import_legacy_field_survey.py --with-photos
    .venv\\Scripts\\python.exe scripts\\import_legacy_field_survey.py --csv path\\to\\local.csv
"""
import argparse
import csv
import http.cookiejar
import io
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from pipeline.db import SourceType, Truck, get_session_factory, init_db, database_url  # noqa: E402
from pipeline.storage import get_storage  # noqa: E402

DEFAULT_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1Q209vN7sg9T42Sf2ocxkl5jTFxD0nE8lDalD7jOHRqI/export?format=csv&gid=2132408911"
)

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
SOURCE_REF_PREFIX = "legacy_field_survey_row_"

_MOBILE_RE = re.compile(r'^[6-9]\d{9}$')
_DRIVE_ID_RE = re.compile(r'(?:id=|/d/)([a-zA-Z0-9_-]{15,})')
_UA = {"User-Agent": "Mozilla/5.0"}
_CONFIRM_RE = re.compile(rb'confirm=([0-9A-Za-z_-]+)')
_FORM_ACTION_RE = re.compile(rb'action="([^"]+)"')
_HIDDEN_INPUT_RE = re.compile(rb'<input type="hidden" name="([^"]+)" value="([^"]*)"')
_EXT_BY_CTYPE = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def _load_csv_text(csv_arg: str) -> str:
    if csv_arg.startswith("http://") or csv_arg.startswith("https://"):
        with urllib.request.urlopen(csv_arg, timeout=30) as resp:
            return resp.read().decode("utf-8")
    return Path(csv_arg).read_text(encoding="utf-8")


def _clean_phone_tokens(raw: str) -> list:
    """Split on common separators, strip everything but digits, strip a leading
    country/std-code prefix, keep only tokens that are a clean 10-digit Indian
    mobile number. Garbage (landlines, truncated/garbled entries) is dropped
    per-token rather than poisoning the whole field."""
    if not raw:
        return []
    out = []
    for tok in re.split(r'[,/;]+', raw):
        digits = re.sub(r'\D', '', tok)
        digits = re.sub(r'^(0091|91|0)(?=\d{10}$)', '', digits)
        if _MOBILE_RE.match(digits):
            out.append(digits)
    return out


def _is_phone_shaped(s: str) -> bool:
    """A 'name' field that's actually just a phone number typed in the wrong
    column (no letters, has digits) — a data-entry slip seen in a few rows."""
    return bool(s) and not re.search(r'[A-Za-z]', s) and bool(re.search(r'\d', s))


def _role_phones(name_val: str, phone_val: str) -> list:
    toks = _clean_phone_tokens(phone_val)
    if not toks and _is_phone_shaped(name_val):
        toks = _clean_phone_tokens(name_val)
    return toks


def _clean_plate(raw: str):
    v = re.sub(r'[^A-Za-z0-9]', '', raw or '').upper()
    return v or None


def _parse_timestamp(raw: str):
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def _extract_drive_id(url: str):
    if not url:
        return None
    m = _DRIVE_ID_RE.search(url)
    return m.group(1) if m else None


def _make_drive_opener():
    """A shared cookiejar across requests — Drive's large-file confirm dance
    expects the confirm request to carry the cookie set on the first response."""
    cj = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def _download_drive_file(opener, file_id: str, timeout=30):
    """Fetch a Drive file anonymously (requires 'Anyone with the link' sharing).
    Handles the 'Google can't scan this file for viruses' HTML interstitial that
    Drive shows for some files regardless of size, by following its confirm form/
    token. Returns (bytes, content_type); raises on anything that isn't image/*."""
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    req = urllib.request.Request(url, headers=_UA)
    resp = opener.open(req, timeout=timeout)
    data = resp.read()
    ctype = resp.headers.get("Content-Type", "")

    if ctype.startswith("text/html"):
        action_m = _FORM_ACTION_RE.search(data)
        if action_m:
            action = action_m.group(1).decode()
            params = {k.decode(): v.decode() for k, v in _HIDDEN_INPUT_RE.findall(data)}
            qs = urllib.parse.urlencode(params)
            sep = "&" if "?" in action else "?"
            url2 = f"{action}{sep}{qs}"
        else:
            confirm_m = _CONFIRM_RE.search(data)
            if not confirm_m:
                raise ValueError("Drive returned an HTML page with no confirm token "
                                  "(file likely not shared 'Anyone with the link')")
            url2 = f"https://drive.google.com/uc?export=download&confirm={confirm_m.group(1).decode()}&id={file_id}"
        req2 = urllib.request.Request(url2, headers=_UA)
        resp2 = opener.open(req2, timeout=timeout)
        data = resp2.read()
        ctype = resp2.headers.get("Content-Type", "")

    if not ctype.startswith("image/"):
        raise ValueError(f"unexpected content-type {ctype!r} ({len(data)} bytes)")
    return data, ctype


def _fetch_row_photos(opener, truck_id: int, drive_urls: list, failures: list, row_num: int):
    """Download each Drive photo for a row and re-host it via pipeline.storage
    under the same reports/<id>/<idx>.ext convention the mobile upload path uses.
    Returns the list of stored keys (may be shorter than drive_urls on failure —
    failures are logged, never fatal to the row)."""
    storage = get_storage()
    keys = []
    for idx, url in enumerate(drive_urls):
        file_id = _extract_drive_id(url)
        if not file_id:
            failures.append((row_num, url, "could not parse a Drive file id from the URL"))
            continue
        for attempt in range(2):
            try:
                data, ctype = _download_drive_file(opener, file_id)
                ext = _EXT_BY_CTYPE.get(ctype, ".jpg")
                key = f"reports/{truck_id}/{idx}{ext}"
                storage.put(key, data, content_type=ctype)
                keys.append(key)
                break
            except (urllib.error.URLError, ValueError, TimeoutError) as e:
                if attempt == 0:
                    time.sleep(1.0)
                    continue
                failures.append((row_num, url, str(e)))
        time.sleep(0.2)  # be polite to Drive's anonymous-download endpoint
    return keys


def _compose_other_text(row: dict, company_used: str, skip_photo_note: bool = False) -> str:
    parts = []
    driver_name = row.get("Driver Name", "")
    if driver_name and not _is_phone_shaped(driver_name):
        parts.append(f"Driver: {driver_name}")
    malik_name = row.get("Motor Malik Name", "")
    if malik_name and not _is_phone_shaped(malik_name) and company_used != "malik":
        parts.append(f"Owner (Motor Malik): {malik_name}")
    transporter_name = row.get("Transporter Name", "")
    if transporter_name and not _is_phone_shaped(transporter_name) and company_used != "transporter":
        parts.append(f"Transporter: {transporter_name}")
    dest = row.get("Truck Destinations and Rate (comma separated)", "")
    if dest:
        parts.append(f"Destinations/Rate: {dest}")

    specs = []
    if row.get("Axel"):
        specs.append(f"Axle: {row['Axel']}")
    if row.get("Weight Capacity in Ton"):
        specs.append(f"Capacity: {row['Weight Capacity in Ton']}T")
    if row.get("Truck Make"):
        specs.append(f"Make: {row['Truck Make']}")
    if row.get("Truck Model"):
        specs.append(f"Model: {row['Truck Model']}")
    length, height = row.get("Length in Feet"), row.get("Height in Feet")
    if length or height:
        specs.append(f"Size: {length or '?'}ft x {height or '?'}ft")
    if specs:
        parts.append(" | ".join(specs))

    if row.get("Remarks"):
        parts.append(f"Remarks: {row['Remarks']}")

    collector = row.get("Person Collecting") or "field team"
    coll_date = row.get("Collection Date") or "?"
    parts.append(f"Collected by {collector} on {coll_date} (sheet row {row['_rownum']})")

    if not skip_photo_note:
        photos = [p for p in (row.get("Truck Photo 1", ""), row.get("Truck Photo 2", "")) if p]
        if photos:
            parts.append("Photos (pending Drive access - not yet imported): " + " ; ".join(photos))

    return "\n".join(parts)


def _build_truck(row: dict, with_photos: bool = False):
    """Returns (Truck, None) to insert, or (None, skip_reason)."""
    remarks = row.get("Remarks", "")
    if "test record" in remarks.lower():
        return None, "marked as a test record in Remarks"

    driver_phones = _role_phones(row.get("Driver Name", ""), row.get("Driver Phone Number", ""))
    malik_phones = _role_phones(row.get("Motor Malik Name", ""), row.get("Motor Malik Phone Number", ""))
    transporter_phones = _role_phones(row.get("Transporter Name", ""), row.get("Transporter Phone Number", ""))

    seen, phones = set(), []
    for p in driver_phones + malik_phones + transporter_phones:
        if p not in seen:
            seen.add(p)
            phones.append(p)
    if not phones:
        return None, "no recoverable 10-digit mobile number in any phone/name field"

    detected_at = _parse_timestamp(row.get("Timestamp", ""))
    if detected_at is None:
        return None, f"unparseable Timestamp {row.get('Timestamp')!r}"

    transporter_name = row.get("Transporter Name", "")
    malik_name = row.get("Motor Malik Name", "")
    company_used = None
    company_name = None
    if transporter_name and not _is_phone_shaped(transporter_name):
        company_name, company_used = transporter_name, "transporter"
    elif malik_name and not _is_phone_shaped(malik_name):
        company_name, company_used = malik_name, "malik"

    num_wheels = None
    raw_wheels = row.get("Total Tuck Wheels", "")
    if raw_wheels:
        try:
            num_wheels = int(raw_wheels)
        except ValueError:
            pass

    truck = Truck(
        detected_at=detected_at,
        source=SourceType.image_api,
        source_ref=f"{SOURCE_REF_PREFIX}{row['_rownum']}",
        license_plate=_clean_plate(row.get("Truck Registration Number", "")),
        plate_confidence=None,
        company_name=company_name or None,
        phone_number="; ".join(phones),
        vehicle_type="TRUCK",
        city=row.get("Photo Data Collection From") or None,
        other_text=_compose_other_text(row, company_used, skip_photo_note=with_photos),
        body_type=row.get("Body Type") or None,
        num_wheels=num_wheels,
        reported_by=row.get("Person Collecting") or None,
        verification_status="VERIFIED",
        review_status="PASSED",
        reviewed_by="Legacy field-survey import",
        reviewed_at=datetime.now(IST),
        review_note="Migrated from legacy field-survey spreadsheet; contacts already called by field team",
        processing_status="DONE",
        processed_at=detected_at,
        image_keys=None,
    )
    return truck, None


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--csv", default=DEFAULT_CSV_URL, help="Sheet CSV export URL or local file path")
    p.add_argument("--dry-run", action="store_true", help="Parse and report only; no DB writes")
    p.add_argument("--with-photos", action="store_true",
                    help="Also download each row's Drive photos and re-host them via pipeline.storage "
                         "(the Drive folder/files must be shared 'Anyone with the link — Viewer')")
    args = p.parse_args()

    print(f"Loading: {args.csv}")
    text = _load_csv_text(args.csv)
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for i, raw_row in enumerate(reader, start=2):  # row 1 is the header
        row = {(k or "").strip(): (v or "").strip() for k, v in raw_row.items()}
        row["_rownum"] = i
        rows.append(row)
    print(f"Parsed {len(rows)} data rows.")

    print(f"Target DB: {database_url()}")
    init_db()
    Session = get_session_factory()
    s = Session()
    try:
        existing_refs = set(s.execute(
            select(Truck.source_ref).where(Truck.source_ref.like(f"{SOURCE_REF_PREFIX}%"))
        ).scalars())

        to_insert, skipped, already = [], [], []
        for row in rows:
            ref = f"{SOURCE_REF_PREFIX}{row['_rownum']}"
            if ref in existing_refs:
                already.append(row["_rownum"])
                continue
            truck, reason = _build_truck(row, with_photos=args.with_photos)
            if truck is None:
                skipped.append((row["_rownum"], row.get("Truck Registration Number") or "(no plate)", reason))
            else:
                to_insert.append((row, truck))

        print(f"\nTo import: {len(to_insert)}")
        print(f"Already imported (source_ref match): {len(already)}")
        print(f"Skipped: {len(skipped)}")
        if skipped:
            print("\nSkipped rows (review these manually if needed):")
            for rownum, plate, reason in skipped:
                print(f"  row {rownum:>4}  {plate:<14}  {reason}")

        if args.dry_run:
            print("\n--dry-run: no changes written.")
            for row, t in to_insert[:5]:
                print(f"  [preview] row={t.source_ref} plate={t.license_plate} "
                      f"phone={t.phone_number} company={t.company_name} city={t.city}")
            if len(to_insert) > 5:
                print(f"  ... and {len(to_insert) - 5} more")
            return

        photo_failures = []
        photo_count = 0
        opener = _make_drive_opener() if args.with_photos else None
        for i, (row, truck) in enumerate(to_insert, start=1):
            s.add(truck)
            s.flush()  # assigns truck.id, needed for the reports/<id>/<idx> storage key
            if args.with_photos:
                drive_urls = [u for u in (row.get("Truck Photo 1", ""), row.get("Truck Photo 2", "")) if u]
                if drive_urls:
                    before = len(photo_failures)
                    keys = _fetch_row_photos(opener, truck.id, drive_urls, photo_failures, row["_rownum"])
                    if keys:
                        truck.image_keys = keys
                        photo_count += len(keys)
                    new_failures = photo_failures[before:]
                    if new_failures:
                        failed_urls = " ; ".join(u for _, u, _ in new_failures)
                        note = f"Photo(s) not imported (Drive access issue): {failed_urls}"
                        truck.other_text = f"{truck.other_text}\n{note}" if truck.other_text else note
            if i % 20 == 0:
                print(f"  ... {i}/{len(to_insert)} rows processed")

        s.commit()
        print(f"\nOK — inserted {len(to_insert)} rows.")
        if args.with_photos:
            print(f"Photos stored: {photo_count}")
            if photo_failures:
                print(f"Photo failures: {len(photo_failures)}")
                for rownum, url, err in photo_failures:
                    print(f"  row {rownum:>4}  {url}  -> {err}")
    finally:
        s.close()


if __name__ == "__main__":
    main()
