"""Geocoding both ways, for mobile reports that only give the backend one half of
the location picture (see pipeline/reports.py::reconcile()):
  - reverse_geocode: GPS coordinates -> a short place name, for Android builds that
    send only latitude/longitude and no typed address.
  - forward_geocode: a typed place name -> GPS coordinates, for the opposite case —
    the reporter typed a location but the app sent no coordinates, which otherwise
    left latitude/longitude NULL and broke the Android app's "show on map" feature
    for that report (it only ever reads latitude/longitude back, never the text).
Both use OpenStreetMap's public Nominatim API — free, no API key. Usage policy caps
this at ~1 request/sec (nominatim.org/release-docs/latest/api/Usage_Policy/); fine
here since at most one of the two fires per field report (never both — each covers
the other's missing half), already serialized behind the single OCR worker thread
(webapp/processing.py).
"""
import logging

import requests

log = logging.getLogger(__name__)

_NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
_NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
_USER_AGENT = "FreightDesk-TruckIntel/1.0 (hr@faberwork.com)"
_TIMEOUT_SEC = 5

# OSM tags several major Indian cities' boundary with the formal name of the civic
# body rather than the plain city name (e.g. Jaipur -> "Jaipur Municipal
# Corporation") — strip that bureaucratic suffix so the Location column reads the
# way a driver/broker would actually say it.
_CIVIC_BODY_SUFFIXES = (
    " Municipal Corporation", " Nagar Nigam", " Municipal Council",
    " Nagar Palika", " Cantonment Board", " Corporation",
)


def _strip_civic_suffix(name: str) -> str:
    for suffix in _CIVIC_BODY_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)].strip()
    return name


def reverse_geocode(latitude, longitude):
    """City name for (latitude, longitude), or None. Never raises — a geocoding
    hiccup should not fail the report."""
    if latitude is None or longitude is None:
        return None
    try:
        resp = requests.get(
            _NOMINATIM_REVERSE_URL,
            params={"format": "jsonv2", "lat": latitude, "lon": longitude, "zoom": 10},
            headers={"User-Agent": _USER_AGENT},
            timeout=_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        addr = resp.json().get("address") or {}
    except Exception:
        log.warning("reverse geocode failed for (%s, %s)", latitude, longitude, exc_info=True)
        return None

    name = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("county")
    return _strip_civic_suffix(name) if name else None


def forward_geocode(location_text):
    """(latitude, longitude) for a typed place name, or None. Never raises — a
    geocoding hiccup should not fail the report. Biased to India (countrycodes=in)
    since every other assumption in this app (plate formats, IST timestamps) is
    already India-specific, and it disambiguates a plain city name like "Jaipur"
    from same-named places elsewhere."""
    if not location_text or not location_text.strip():
        return None
    try:
        resp = requests.get(
            _NOMINATIM_SEARCH_URL,
            params={"q": location_text, "format": "jsonv2", "limit": 1, "countrycodes": "in"},
            headers={"User-Agent": _USER_AGENT},
            timeout=_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        results = resp.json()
    except Exception:
        log.warning("forward geocode failed for %r", location_text, exc_info=True)
        return None
    if not results:
        return None
    try:
        return float(results[0]["lat"]), float(results[0]["lon"])
    except (KeyError, ValueError, TypeError):
        return None
