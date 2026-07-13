"""Reverse geocoding: turn GPS coordinates into a short place name.

Fallback for mobile reports whose Android build sends only latitude/longitude and
no typed address (see pipeline/reports.py::reconcile()). Uses OpenStreetMap's public
Nominatim API — free, no API key. Usage policy caps this at ~1 request/sec
(nominatim.org/release-docs/latest/api/Usage_Policy/); fine here since it's only
called once per field report, already serialized behind the single OCR worker
thread (webapp/processing.py).
"""
import logging

import requests

log = logging.getLogger(__name__)

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
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
            _NOMINATIM_URL,
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
