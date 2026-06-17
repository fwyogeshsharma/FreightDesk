"""Pure text-extraction logic shared by every output sink (CSV, database, API).

`extract_truck_fields(event)` turns a closed `TruckEvent` into the structured
content fields (plate, company, phone, website, type, city, other text). It holds
NO output-format concerns — no sequential ids, no run-progress, no CSV/DB shape —
so the CSV writer, the Postgres writer, and the image API all call the exact same
code and produce identical extraction.
"""
import re
from typing import List, Optional, Tuple

from .utils import normalize_text

# ── Extraction patterns ────────────────────────────────────────────────────────

# Indian mobile: 10 digits starting 6-9, optional +91/0091 prefix stripped first
_PHONE_RE = re.compile(r'([6-9]\d{9})')

# URLs / websites
_WEB_RE = re.compile(
    r'\b([a-zA-Z][a-zA-Z0-9\-]*\.(?:com|in|co\.in|net|org|info|biz)(?:/[^\s]*)?)\b',
    re.IGNORECASE,
)

# Email addresses (OCR often drops the dot in the domain: "info@rrroadwayspvtltdcom")
_EMAIL_RE = re.compile(r'([a-zA-Z0-9._+\-]+@[a-zA-Z0-9\-]+(?:\.[a-zA-Z]{2,})?)')

# Known vehicle types — checked in order (most specific first)
_VEHICLE_TYPE_RULES: List[Tuple[str, str]] = [
    (r'\bSCHOOL\b.*\bBUS\b|\bSCHOOL\s+BUS\b', 'SCHOOL BUS'),
    (r'\bOIL\s+TANKER\b|\bFUEL\s+TANKER\b', 'OIL TANKER'),
    (r'\bTANKER\b', 'TANKER'),
    (r'\bTIPPER\b', 'TIPPER'),
    (r'\bCONTAINER\b', 'CONTAINER'),
    (r'\bMAXI\s*CAB\b|\bMAXICAB\b', 'MAXICAB'),
    (r'\bGOODS\s+CARRIER\b|\bGOODS\b', 'GOODS CARRIER'),
    (r'\bSCHOOL\b', 'SCHOOL BUS'),   # "SCHOOL" alone strongly implies school bus
    (r'\bBUS\b', 'BUS'),
    (r'\bTRUCK\b|\bLORRY\b', 'TRUCK'),
]
_VEHICLE_PATTERNS = [(re.compile(p, re.IGNORECASE), label) for p, label in _VEHICLE_TYPE_RULES]

# Known cities/routes that appear on Indian trucks
_CITY_RE = re.compile(
    r'\b(JAIPUR|DELHI|MUMBAI|KOLKATA|CHENNAI|BANGALORE|BENGALURU|'
    r'HYDERABAD|PUNE|AHMEDABAD|LUCKNOW|AGRA|AJMER|JODHPUR|UDAIPUR|'
    r'KOTA|BIKANER|ALWAR|BHARATPUR|SIKAR|SURAT|CHANDIGARH|AMRITSAR|'
    r'INDORE|BHOPAL|NAGPUR|COIMBATORE|PATNA|RANCHI|BHUBANESWAR)\b',
    re.IGNORECASE,
)

# Company name: text that ends with a known corporate suffix.
# No leading \b — OCR often merges words ("RRROADWAYS" should still match)
_COMPANY_SUFFIX_RE = re.compile(
    r'(TRANSPORT(S)?|LOGISTICS|ROADWAYS|ROADWAY|CARRIERS?|SERVICES?|'
    r'ENTERPRISES?|INDUSTRIES|INDUSTRY|MOVERS?|PACKERS?|TRADERS?|'
    r'AGENCY|AGENCIES|TOURS?|TRAVELS?|MOTORS?|AUTOMOTIVE|'
    r'PVT\.?\s*LTD\.?|LTD\.?|LIMITED|CORPORATION|CORP\.?|'
    r'INTERNATIONAL|NATIONAL|EXPRESS|FREIGHT|CARGO|LINES?|'
    r'CONSTRUCTION|INFRA|INFRASTRUCTURE|SCHOOL)\s*$',
    re.IGNORECASE,
)

# Standalone noise words — skip these when picking company name
_NOISE_STANDALONE = frozenset([
    'BUS', 'FOR', 'BY', 'THE', 'AND', 'WITH', 'FROM', 'TO', 'OF', 'IN', 'AT',
    'PC', 'APC', 'UR', 'CO', 'JAI', 'CNG', 'JUS', 'FORE', 'FORC', 'TFORC',
    'OWNED', 'OPERATED', 'OPERATING', 'PUBLIC', 'PVT', 'LTD', 'SCHOOL',
    'TRANSPORT', 'CARRIER', 'LOGISTICS', 'BUY', 'CAB', 'MAXI', 'RUN',
])

# Characters that mark OCR noise at start or end of a token
_LEADING_NOISE = re.compile(r"^['\"\[{(\~_`,\.]+")
_TRAILING_NOISE = re.compile(r"['\"\]})=:;~_`\|]+$")


# ── Helper functions ───────────────────────────────────────────────────────────

def _levenshtein(a: str, b: str) -> int:
    try:
        from Levenshtein import distance
        return distance(a, b)
    except ImportError:
        pass
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1,
                            prev[j] + (0 if ca == cb else 1)))
        prev = curr
    return prev[-1]


def _clean_token(text: str) -> str:
    """Strip leading/trailing OCR noise characters."""
    t = _LEADING_NOISE.sub('', text).strip()
    t = _TRAILING_NOISE.sub('', t).strip()
    return t


def _is_noise(text: str) -> bool:
    """True if the text is too short, purely numeric, or obvious garbage."""
    clean = re.sub(r'[^A-Z0-9]', '', text.upper())
    if len(clean) < 2:
        return True
    # Pure digit strings: keep only if they contain a full 10-digit mobile number
    # (otherwise they're OSD clock fragments or partial reads)
    if re.match(r'^\d+$', clean) and not _PHONE_RE.search(clean):
        return True
    return False


def deduplicate(texts: List[Tuple[str, float]]) -> List[str]:
    """Deduplicate OCR texts: normalize, drop noise, collapse near-duplicates."""
    normalized: dict = {}
    for text, conf in texts:
        n = normalize_text(_clean_token(text))
        if _is_noise(n):
            continue
        if n not in normalized or conf > normalized[n]:
            normalized[n] = conf

    unique = list(normalized.keys())
    to_remove = set()
    for i, a in enumerate(unique):
        if a in to_remove:
            continue
        for j, b in enumerate(unique):
            if i == j or b in to_remove:
                continue
            if abs(len(a) - len(b)) <= 1 and _levenshtein(a, b) <= 1:
                to_remove.add(a if len(a) <= len(b) else b)

    return sorted(t for t in unique if t not in to_remove)


# OCR letter-for-digit confusions, applied only inside digit-dominated strings
_DIGIT_CONFUSION = str.maketrans('IilL|OoSsBZz', '111110058822')


def _extract_phones(texts: List[str]) -> str:
    """Extract Indian phone numbers (10 digits, start 6-9).
    OCR misreads single digits, so the same painted number yields variants across
    frames — collapse numbers within Levenshtein distance 2, keeping the variant
    that was read most often."""
    from collections import Counter
    counts: Counter = Counter()
    for t in texts:
        # In digit-heavy strings, fix common OCR confusions ("981i008120" -> "9811008120")
        if sum(c.isdigit() for c in t) >= 6:
            t = t.translate(_DIGIT_CONFUSION)
        # Keep only digits ("HO,98333-72790" -> "9833372790"), strip country code
        digits = re.sub(r'\D', '', t)
        digits = re.sub(r'^0091|^91(?=\d{10})|^0(?=\d{10})', '', digits)
        for m in _PHONE_RE.finditer(digits):
            counts[m.group(1)] += 1

    kept: List[str] = []
    for num, _ in counts.most_common():
        if all(_levenshtein(num, k) > 2 for k in kept):
            kept.append(num)
    return '; '.join(sorted(kept))


def _extract_websites(texts: List[str]) -> str:
    """Extract website URLs and email addresses."""
    sites = set()
    for t in texts:
        for m in _WEB_RE.finditer(t):
            sites.add(m.group(1).lower())
        # Emails: collapse spaces around @ first ("Emailinfo @rrroadways" variants)
        t_email = re.sub(r'\s*@\s*', '@', t)
        for m in _EMAIL_RE.finditer(t_email):
            e = m.group(1).lower()
            e = re.sub(r'^e?mail', '', e)  # strip a leading "Email" label read as part of the address
            if len(e.split('@')[1]) >= 4:
                sites.add(e)
    return '; '.join(sorted(sites))


# YOLO COCO class id -> default vehicle type label
# (6="train" is almost always a tanker/long truck misclassified)
_YOLO_CLASS_LABEL = {5: 'BUS', 6: 'TRUCK', 7: 'TRUCK'}


def _detect_vehicle_type(texts: List[str], class_votes=None) -> str:
    """Detect vehicle type from text keywords, falling back to the YOLO class."""
    joined = ' '.join(texts)
    for pattern, label in _VEHICLE_PATTERNS:
        if pattern.search(joined):
            return label
    if class_votes:
        top_class = class_votes.most_common(1)[0][0]
        return _YOLO_CLASS_LABEL.get(top_class, '')
    return ''


def _extract_cities(texts: List[str]) -> str:
    """Extract city/route names."""
    found = set()
    for t in texts:
        for m in _CITY_RE.finditer(t):
            found.add(m.group(1).title())
    return '; '.join(sorted(found))


def _extract_company_name(texts: List[str]) -> str:
    """
    Best-effort company name extraction.
    Priority: longest text ending with a known corporate suffix,
    then longest clean multi-word phrase, then longest clean single word.
    """
    # Build city set first so we can exclude city-only tokens
    city_upper = {m.group(1).upper()
                  for t in texts for m in _CITY_RE.finditer(t)}

    candidates = []
    for raw in texts:
        t = _clean_token(raw)
        if not t or not t[0].isalpha():
            continue
        clean_alpha = re.sub(r'[^A-Z0-9]', '', t.upper())
        if len(clean_alpha) < 4:
            continue
        # Skip pure noise standalone words and bare city names
        if t.upper() in _NOISE_STANDALONE or t.upper() in city_upper:
            continue
        # Skip pure-digit or timestamp-like strings
        if re.match(r'^[\d\s\-\./:\\]+$', t):
            continue
        # Skip digit-dominated strings (phone fragments like "HO 9833727900")
        if sum(c.isdigit() for c in clean_alpha) > sum(c.isalpha() for c in clean_alpha):
            continue
        # Skip strings with 3+ consecutive digits — plate/fleet-number reads,
        # not company names (e.g. "FRJASCR2455", "RDTAPE 2350")
        if re.search(r'\d{3}', t):
            continue
        # Skip URLs (they go in the website column)
        if _WEB_RE.search(t):
            continue
        # Skip email-like fragments ("Emailinfo @rrroadways...")
        if '@' in t or t.upper().startswith(('EMAIL', 'WWW', 'MAIL')):
            continue
        words = t.split()
        # Skip single long words — likely merged OCR garbage (e.g. "TDELHIPUBLICSCHOOL")
        if len(words) == 1 and len(clean_alpha) > 12:
            continue
        # Skip vowel-free single words — smashed abbreviations (e.g. "PVTLTD", "TFORC")
        if len(words) == 1:
            if sum(1 for c in clean_alpha if c in 'AEIOU') == 0:
                continue
        # Skip multi-word phrases where any word is a single character (e.g. "DELHI L")
        if len(words) > 1 and any(len(w) < 2 for w in words):
            continue
        candidates.append(t)

    if not candidates:
        return ''

    def _score(t):
        suffix = 1 if _COMPANY_SUFFIX_RE.search(t) else 0
        words = len(t.split())
        return (suffix, words, len(t))

    candidates.sort(key=_score, reverse=True)

    # Return top result; if there's a clear second that isn't a substring, add it
    result = [candidates[0]]
    top_upper = candidates[0].upper()
    for c in candidates[1:]:
        c_upper = c.upper()
        if c_upper not in top_upper and top_upper not in c_upper:
            result.append(c)
            break

    return '; '.join(result)


def _extract_other(texts: List[str], exclude: List[str]) -> str:
    """Return clean text that wasn't claimed by any specific extractor."""
    exclude_upper = {e.upper() for e in exclude}
    out = []
    for raw in texts:
        t = _clean_token(raw)
        if not t:
            continue
        t_upper = t.upper()
        if t_upper in exclude_upper:
            continue
        if _is_noise(t):
            continue
        # Skip noise standalone words (BUS, FOR, OWNED, OPERATING, etc.)
        if t_upper in _NOISE_STANDALONE:
            continue
        # Skip pure numeric strings
        if re.match(r'^[\d\s\-\.]+$', t):
            continue
        clean_alpha = re.sub(r'[^A-Z0-9]', '', t_upper)
        # Skip very short tokens
        if len(clean_alpha) < 4:
            continue
        # Skip digit-dominated strings (phone fragments — already extracted)
        if sum(c.isdigit() for c in clean_alpha) > sum(c.isalpha() for c in clean_alpha):
            continue
        words = t.split()
        # Skip single long words (likely merged OCR garbage)
        if len(words) == 1 and len(clean_alpha) > 12:
            continue
        # Skip vowel-free single words (smashed abbreviations)
        if len(words) == 1 and sum(1 for c in clean_alpha if c in 'AEIOU') == 0:
            continue
        # Skip bare city names (they have their own column)
        m = _CITY_RE.search(t)
        if m and m.group().strip().lower() == t.strip().lower():
            continue
        out.append(t)
    return '; '.join(dict.fromkeys(out))  # preserve order, deduplicate


# ── Noise gate ─────────────────────────────────────────────────────────────────

def is_noise_event(event, body_unique: List[str] = None) -> bool:
    """A brief detection with no plate and no readable text — a false positive."""
    if body_unique is None:
        body_unique = deduplicate(event.body_texts)
    return not event.best_plate and not body_unique and event.frame_count < 5


# ── The shared field extractor ──────────────────────────────────────────────────

def extract_truck_fields(event) -> Optional[dict]:
    """Turn a closed TruckEvent into structured content fields.

    Returns None for noise events (no plate, no text, seen only briefly). The
    returned dict holds identity/content only — NOT sequential ids or run
    progress, which are the responsibility of each individual sink.
    """
    plate_texts = [(p, 1.0) for p in event.plate_candidates.keys()]
    body_raw = event.body_texts  # list of (text, conf)
    body_unique = deduplicate(body_raw)

    if is_noise_event(event, body_unique):
        return None

    company = _extract_company_name(body_unique)
    # Raw (pre-dedup) texts give repeat counts for collapsing phone variants
    phone = _extract_phones([t for t, _ in body_raw])
    website = _extract_websites(body_unique)
    vtype = _detect_vehicle_type(body_unique, getattr(event, 'class_votes', None))
    city = _extract_cities(body_unique)

    # other_text = everything not captured above
    claimed = []
    if company:
        claimed.extend(company.split('; '))
    if city:
        claimed.extend(c for c in body_unique if _CITY_RE.search(c))
    other = _extract_other(body_unique, claimed)

    plate_conf_count = sum(event.plate_candidates.values())
    if plate_conf_count == 0:
        plate_conf = 'NONE'
    elif plate_conf_count >= 3:
        plate_conf = 'HIGH'
    else:
        plate_conf = 'LOW'

    return {
        'license_plate':    event.best_plate or '',
        'plate_confidence': plate_conf,
        'company_name':     company,
        'phone_number':     phone,
        'website':          website,
        'vehicle_type':     vtype,
        'city':             city,
        'other_text':       other,
        'frames':           event.frame_count,
        'first_seen_sec':   event.start_time,
        'last_seen_sec':    event.last_seen,
        'source_video':     event.source_video,
        'plate_candidates': dict(event.plate_candidates),
        'body_texts':       body_unique,
    }


def fmt_ts(seconds: float) -> str:
    """Format seconds as HH:MM:SS."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"
