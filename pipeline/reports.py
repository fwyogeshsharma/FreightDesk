"""Reconcile a mobile field report (user-typed fields) with what OCR read off the
photos, producing the final `trucks` row + a verification verdict.

Contributors are anonymous and paid for correct uploads, so nothing is trusted on
the user's word — the photos are the only independent proof.

Rules (from the product owner):
- Verification: a report is VERIFIED only when the photos confirm the typed vehicle
  number (fuzzy plate match — OCR is noisy). No plate read, no number typed, or a
  plate that disagrees → the row is still stored but UNVERIFIED (not auto-trusted /
  not auto-paid), with a reason recorded for abuse review.
- Phone (mandatory): merge the user-typed number with any OCR-read numbers, keeping a
  column-level distinction of reported vs OCR.
- Everything else OCR finds (company, vehicle type, city, other text) flows to its
  own column; the remaining fields are taken verbatim from the report.
"""
import re
from datetime import datetime
from typing import Optional

from .extract import _extract_phones, _levenshtein

# Tight normalization for plate comparison: letters+digits only, uppercased.
_ALNUM = re.compile(r'[^A-Z0-9]')


def _norm_plate(s: str) -> str:
    return _ALNUM.sub('', (s or '').upper())


# Letter/digit pairs OCR commonly confuses on plates (look-alike glyphs, esp. at an
# angle or in low light). Folding both sides onto the same symbol before comparing
# lets a near-miss like 'G' vs '6' count as equal instead of a full substitution.
_CONFUSABLES = str.maketrans({'G': '6', 'O': '0', 'I': '1', 'S': '5', 'B': '8', 'Z': '2', 'Q': '0'})


def _fold_confusables(s: str) -> str:
    return s.translate(_CONFUSABLES)


def _plate_fragments(ocr: dict) -> list:
    """Normalized alnum OCR tokens that could be the plate, or one line of it.
    Body-text tokens must contain a digit to qualify, so plain company words aren't
    spuriously concatenated into a 'plate'. (Plate lines always carry digits.)"""
    frags, seen = [], set()

    def add(s, require_digit=False):
        n = _norm_plate(s)
        if not (2 <= len(n) <= 12):
            return
        if require_digit and not any(c.isdigit() for c in n):
            return
        if n not in seen:
            seen.add(n)
            frags.append(n)

    add(ocr.get("license_plate") or "")               # dedicated plate-detector read
    for k in (ocr.get("plate_candidates") or {}):       # all plate candidates
        add(k)
    for t in (ocr.get("body_texts") or []):             # body OCR (digit-bearing only)
        add(t, require_digit=True)
    return frags


def _match_reported_plate(n_rep: str, ocr: dict, max_dist: int):
    """Confirm a typed plate against the OCR fragments, **reassembling split plates**
    (Indian plates are often painted on two lines, so OCR yields 'OR02' and 'AS3344'
    separately — neither matches alone, but joined they do). Returns the matched OCR
    string, or None if nothing is within max_dist."""
    frags = _plate_fragments(ocr)
    if not n_rep or not frags:
        return None
    candidates = set(frags)
    for a in frags:                       # reassemble two-line plates: ordered pairs
        for b in frags:
            if a != b:
                candidates.add(a + b)
    candidates.add("".join(frags))        # and all fragments joined, in read order
    best = min(candidates, key=lambda c: _levenshtein(n_rep, c))
    if _levenshtein(n_rep, best) <= max_dist:
        return best

    # Single-line-only reads: plates are painted on two lines, and OCR often catches
    # just one of them (usually the bottom series+number, occasionally the top state/
    # RTO code) while missing the other entirely — the missing line alone blows the
    # edit-distance budget even when the captured line is a clean read. If a fragment
    # EXACTLY matches the corresponding slice of the reported plate (head or tail)
    # once common OCR look-alike swaps are folded away (G/6, O/0, I/1, S/5, B/8, Z/2),
    # count it as confirmed — a telecaller verifies every report by phone before it's
    # reward-eligible anyway. Exact-after-folding (not fuzzy) so a coincidental
    # one-character difference that ISN'T a look-alike swap still counts as a mismatch.
    for frag in frags:
        if 4 <= len(frag) < len(n_rep):
            f = _fold_confusables(frag)
            if f == _fold_confusables(n_rep[-len(frag):]) or f == _fold_confusables(n_rep[:len(frag)]):
                return frag
    return None


def _clean_phone(s: str) -> str:
    """Canonical 10-digit Indian mobile, or '' if it isn't one."""
    got = _extract_phones([s or ''])
    return got.split('; ')[0] if got else ''


def _merge_phones(reported: str, ocr_joined: str) -> tuple[str, str]:
    """Return (merged '; '-joined, status). status: MATCH / MERGED / REPORTED_ONLY."""
    ocr_list = [p for p in (ocr_joined or '').split('; ') if p]
    if not ocr_list:
        return reported, "REPORTED_ONLY"
    # Does the reported number already match one OCR number (fuzzily)?
    matches_existing = any(_levenshtein(reported, o) <= 2 for o in ocr_list) if reported else False
    merged: list[str] = []
    for num in ([reported] if reported else []) + ocr_list:
        if num and all(_levenshtein(num, k) > 2 for k in merged):
            merged.append(num)
    status = "MATCH" if matches_existing and len(merged) == len(ocr_list) else "MERGED"
    return '; '.join(merged), status


def reconcile(reported: dict, ocr: dict, config) -> dict:
    """reported: user form fields. ocr: extract_truck_fields(event) output (or {}).
    Returns the column dict for one `trucks` row (plus plate_status/phone_status),
    or raises ReportRejected."""
    ocr = ocr or {}

    # ── Vehicle number → verification (the trust gate) ──────────────────────────
    # Contributors are anonymous and paid, so we only TRUST a report when the photos
    # independently confirm the typed plate. Everything else is stored but UNVERIFIED
    # (not auto-trusted/paid) with a reason logged for abuse review.
    reported_vn = (reported.get("vehicle_number") or "").strip()
    ocr_plate = (ocr.get("license_plate") or "").strip()
    n_rep = _norm_plate(reported_vn)

    # Confirm the typed plate against ALL plate signals OCR found (plate detector +
    # candidates + digit-bearing body text), reassembling two-line plates.
    matched = _match_reported_plate(n_rep, ocr, config.plate_match_distance) if n_rep else None
    ocr_has_plate_text = bool(_plate_fragments(ocr))

    if n_rep and matched:
        verification_status, plate_status = "VERIFIED", "VERIFIED"
        reason = "vehicle number confirmed from photos"
        license_plate = reported_vn
    elif n_rep and ocr_has_plate_text:
        verification_status, plate_status = "UNVERIFIED", "MISMATCH"
        read = ocr_plate or '/'.join(_plate_fragments(ocr)[:3])
        reason = (f"plate mismatch: reported '{reported_vn}', "
                  f"photos read '{read}'")
        license_plate = reported_vn  # keep the claim; it's just unconfirmed
    elif reported_vn:
        verification_status, plate_status = "UNVERIFIED", "REPORTED"
        reason = "no plate readable in the photos to confirm the vehicle number"
        license_plate = reported_vn
    elif ocr_plate:
        verification_status, plate_status = "UNVERIFIED", "OCR_ONLY"
        reason = "no vehicle number reported by the user"
        license_plate = ocr_plate
    else:
        verification_status, plate_status = "UNVERIFIED", "NONE"
        reason = "no vehicle number reported and none readable in the photos"
        license_plate = None

    # ── Phone (mandatory) ───────────────────────────────────────────────────────
    phone_reported = _clean_phone(reported.get("phone_number", "")) or \
        re.sub(r'\D', '', reported.get("phone_number", "")) or None
    phone_ocr = ocr.get("phone_number") or None
    merged, phone_status = _merge_phones(phone_reported or "", phone_ocr or "")
    phone_number = merged or phone_reported

    # ── Other report fields ─────────────────────────────────────────────────────
    loaded = (reported.get("loaded_status") or "").strip().upper()
    loaded_status = loaded if loaded in ("LOADED", "UNLOADED") else None

    return {
        "detected_at":      _parse_dt(reported.get("captured_at")),
        "source_ref":       reported.get("reported_by") or "mobile_report",
        "license_plate":    license_plate,
        "plate_confidence": plate_status,
        "company_name":     ocr.get("company_name") or None,
        "vehicle_type":     ocr.get("vehicle_type") or None,
        "city":             ocr.get("city") or None,
        "other_text":       ocr.get("other_text") or None,
        "website":          ocr.get("website") or None,
        "phone_number":     phone_number,
        "phone_reported":   phone_reported,
        "phone_ocr":        phone_ocr,
        "loaded_status":    loaded_status,
        "location":         (reported.get("location") or "").strip() or None,
        "latitude":         reported.get("latitude"),
        "longitude":        reported.get("longitude"),
        "num_wheels":       reported.get("number_of_wheels"),
        "reported_by":      (reported.get("reported_by") or "").strip() or None,
        # Reporter identity snapshot (set when the contributor was logged in).
        "reported_by_user_id": reported.get("reported_by_user_id"),
        "reporter_phone":   (reported.get("reporter_phone") or "").strip() or None,
        "verification_status": verification_status,
        "frames":           ocr.get("frames", 0) or 0,
        "plate_candidates": ocr.get("plate_candidates") or None,
        "body_texts":       ocr.get("body_texts") or None,
        # audit/response metadata (not all are trucks columns):
        "reason":           reason,
        "vehicle_reported": reported_vn or None,
        "vehicle_ocr":      ocr_plate or None,
        "plate_status":     plate_status,
        "phone_status":     phone_status,
    }


def _parse_dt(s: Optional[str]) -> datetime:
    if not s:
        return datetime.now()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.now()
