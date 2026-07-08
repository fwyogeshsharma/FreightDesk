"""Broker-lead grouping for the Truck Sightings page (`/`) only.

The Review Queue (`/review`) stays strictly report-level and never imports this
module — one queue item is always one submitted report there. This module exists
purely to make the *broker* list collapse repeat sightings of what looks like the
same real-world truck into one actionable row, instead of showing a broker several
near-identical rows for one truck that happened to get sighted/reported more than
once.

Conservative by design: under-grouping (a few extra rows) is preferred over
over-grouping (silently hiding a distinct lead a broker might want to call). See
`group_broker_rows` for the exact rule.
"""
from typing import Optional

from pipeline.reports import _norm_plate as _normalize_plate_text

# Shorter than this after stripping non-alnum chars -> too short to trust as a
# distinguishing plate for grouping (could be a noisy OCR fragment that
# coincidentally collides with an unrelated truck).
_MIN_RELIABLE_PLATE_LEN = 6

# Fields backfilled from an older sibling report when the newest one is blank —
# "prefer the latest non-empty useful value in the group" from the spec.
_BACKFILL_FIELDS = (
    "license_plate", "company_name", "vehicle_type", "location", "city",
    "loaded_status", "body_type", "material_type", "other_text",
)


def lead_phone(truck: dict) -> Optional[str]:
    """The lead's anchor phone: the first (best) number in the already-cleaned,
    '; '-joined phone_number field. None if there's no usable phone."""
    raw = (truck.get("phone_number") or "").split(";")[0].strip()
    return raw or None


def lead_plate(truck: dict) -> Optional[str]:
    """Normalized plate, or None if missing/too short to trust for grouping."""
    n = _normalize_plate_text(truck.get("license_plate") or "")
    return n if len(n) >= _MIN_RELIABLE_PLATE_LEN else None


def _merge_phones(members: list) -> str:
    """Union of every phone across the group, newest-report-first, deduped —
    so the existing `phone_number | phones` template filter keeps working
    unchanged, and phones[0] is naturally 'the most recent report's number'."""
    seen, merged = set(), []
    for m in members:
        for p in (m.get("phone_number") or "").split(";"):
            p = p.strip()
            if p and p not in seen:
                seen.add(p)
                merged.append(p)
    return "; ".join(merged)


def _build_lead(members: list) -> dict:
    """One broker-facing row from a cluster of raw truck dicts (already sorted
    newest-first by the caller)."""
    newest = members[0]
    lead = dict(newest)  # start from the newest report, then backfill gaps

    for field in _BACKFILL_FIELDS:
        if not lead.get(field):
            for m in members:
                if m.get(field):
                    lead[field] = m[field]
                    break

    lead["phone_number"] = _merge_phones(members)

    # Trust: reuse the EXISTING model (see index.html's Trust column), just
    # extended to group precedence — no new trust states invented.
    non_report = next((m for m in members if m.get("source") != "image_api"), None)
    if non_report is not None:
        # A trusted system-observation (video/stream) exists for this lead —
        # the current per-row template logic already always shows "Auto
        # Verified" for non-image_api sources regardless of review_status,
        # so keeping that source here reproduces that behaviour unchanged.
        lead["source"] = non_report["source"]
    else:
        statuses = {m.get("review_status") for m in members}
        if "PASSED" in statuses:
            lead["review_status"] = "PASSED"
        elif "REJECTED" in statuses:
            lead["review_status"] = "REJECTED"
        else:
            lead["review_status"] = newest.get("review_status")

    lead["_members"] = members          # full history, newest first — for the drawer
    lead["group_count"] = len(members)
    return lead


def group_broker_rows(trucks: list) -> list:
    """Collapse `trucks` (Truck.as_dict()-shaped dicts, already filtered and
    enriched with time_ago/detected_at_human/fresh) into broker lead rows.

    Grouping key, most-conservative-first:
      1. phone + plate — the safe case: same contact AND same vehicle.
      2. phone alone   — only when this phone maps to a SINGLE plate (or none)
                          across the candidate set; if the same phone shows up
                          against 2+ DIFFERENT plates (e.g. a dispatcher's
                          number printed on many trucks), split by plate
                          instead so distinct trucks aren't silently merged.
      3. plate alone   — no usable phone anywhere for this truck.
      4. neither       — kept as its own, ungrouped row.

    Returns a list of lead dicts shaped exactly like the existing per-truck
    dicts the index.html table already reads, sorted newest-first within each
    group, plus `_members` (full group, newest first) and `group_count`.
    """
    by_phone: dict = {}
    no_phone: list = []
    for t in trucks:
        phone = lead_phone(t)
        (by_phone.setdefault(phone, []) if phone else no_phone).append(t)

    clusters: list = []

    for members in by_phone.values():
        plates = {p for p in (lead_plate(m) for m in members) if p}
        if len(plates) <= 1:
            clusters.append(members)
        else:
            by_plate: dict = {}
            for m in members:
                p = lead_plate(m)
                if p:
                    by_plate.setdefault(p, []).append(m)
                else:
                    clusters.append([m])  # ambiguous phone, no plate to anchor it
            clusters.extend(by_plate.values())

    by_plate_only: dict = {}
    for t in no_phone:
        p = lead_plate(t)
        if p:
            by_plate_only.setdefault(p, []).append(t)
        else:
            clusters.append([t])
    clusters.extend(by_plate_only.values())

    leads = []
    for members in clusters:
        members = sorted(members, key=lambda m: m.get("detected_at") or "", reverse=True)
        leads.append(_build_lead(members))
    return leads


def lead_trust(lead: dict) -> str:
    """The Trust badge category a lead renders as on the broker page — one of
    'auto_verified' / 'verified' / 'pending' / 'rejected'. Mirrors index.html's
    Trust column conditionals exactly, so it can be used as a filter that always
    matches what's actually displayed."""
    if lead.get("source") != "image_api":
        return "auto_verified"
    if lead.get("review_status") == "PASSED":
        return "verified"
    if lead.get("review_status") == "REJECTED":
        return "rejected"
    return "pending"


def find_lead_members(candidates: list, representative_id) -> list:
    """Given a broader candidate pool (e.g. every truck sharing the clicked
    row's phone, fetched independently by the caller — see truck_panel()),
    re-run the exact same clustering and return the specific group that
    contains `representative_id`, newest first. Falls back to just the
    representative alone if something upstream doesn't line up."""
    for lead in group_broker_rows(candidates):
        if any(m["id"] == representative_id for m in lead["_members"]):
            return lead["_members"]
    return [c for c in candidates if c["id"] == representative_id]
