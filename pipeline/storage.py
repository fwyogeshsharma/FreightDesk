"""Photo storage for mobile field reports.

**Photos are retained indefinitely** (changed 2026-09-14 — they used to be deleted
after ~2 days, and a lot of prose in this repo still assumed that; see DEPLOY.md for
the history). `IMAGE_RETENTION_DAYS` controls it:

- ``0`` (the default) — keep forever, never sweep.
- ``> 0``             — delete anything older than that many days.

Two interchangeable backends, selected by `IMAGE_STORAGE_BACKEND`:

- ``local`` (dev) — files under a base dir; when retention is enabled a sweeper deletes
  anything older than the window. Env: ``IMAGE_STORAGE_DIR`` (default ``<root>/uploads``).
- ``gcs`` (prod) — a Google Cloud Storage bucket. Expiry there is a property of the
  **bucket's lifecycle rule**, never of this code, so `IMAGE_RETENTION_DAYS` is ignored
  by this backend in both directions: setting it cannot delete a GCS object, and the
  bucket rule will delete objects no matter what it says. The prod bucket currently has
  **no** lifecycle rule (objects kept forever); change retention there with
  ``gcloud storage buckets update --lifecycle-file=...``, not with this env var.
  Env: ``GCS_BUCKET`` (required), ``GCS_PREFIX`` (optional key prefix).

Both implement the same tiny interface: ``put(key, data, content_type)``,
``get(key) -> bytes|None``, ``delete(key)``, ``purge_expired()``.

Keys are **opaque to this module** — it only ever stores and fetches what it is handed.
The upload path currently mints ``reports/<YYYY-MM-DD>/<truck_id>/<idx>.jpg``: the date
comes first so one day's photos sit under a single prefix, which is what makes "delete
everything from that day" one console click or one ``gcloud storage rm -r``.

Two earlier layouts are still present in the bucket and still resolve:
``reports/<truck_id>/<idx>.jpg`` (before 2026-09-14) and
``reports/<truck_id>_<YYYY-MM-DD>/<idx>.jpg`` (briefly, on 2026-09-14).
Nothing breaks, because a photo is always fetched by the key recorded in
``trucks.image_keys``, never by rebuilding one from a pattern — which is also why the
layout could change twice without a migration. Don't add code that parses a key.
"""
import os
import time
from pathlib import Path
from typing import Optional

# Project root (folder containing pipeline/).
_ROOT = Path(__file__).resolve().parent.parent

# 0 (the default) = keep photos forever. Any positive value = delete after that many days.
RETENTION_DAYS = float(os.environ.get("IMAGE_RETENTION_DAYS", "0") or 0)
RETENTION_SECONDS = RETENTION_DAYS * 86400


def retention_enabled() -> bool:
    """True when photos are set to expire. False = keep forever (the default)."""
    return RETENTION_DAYS > 0


class LocalStorage:
    """Filesystem backend for local development. Owns its own expiry sweep."""

    def __init__(self, base_dir: Optional[str] = None):
        self.base = Path(base_dir or os.environ.get("IMAGE_STORAGE_DIR")
                         or (_ROOT / "uploads"))
        self.base.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Keep keys inside the base dir (defend against traversal in a key).
        p = (self.base / key).resolve()
        if not str(p).startswith(str(self.base.resolve())):
            raise ValueError(f"unsafe storage key: {key!r}")
        return p

    def put(self, key: str, data: bytes, content_type: Optional[str] = None) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def get(self, key: str) -> Optional[bytes]:
        p = self._path(key)
        return p.read_bytes() if p.exists() else None

    def delete(self, key: str) -> None:
        p = self._path(key)
        if p.exists():
            p.unlink()

    def purge_expired(self) -> int:
        """Delete files older than the retention window. Returns count removed.

        No-op when retention is disabled (IMAGE_RETENTION_DAYS=0, the default) — the
        guard matters, since without it a 0-day window would mean "older than now",
        i.e. delete every photo the moment it lands.
        """
        if not retention_enabled():
            return 0
        cutoff = time.time() - RETENTION_SECONDS
        removed = 0
        for f in self.base.rglob("*"):
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                pass
        return removed


class GCSStorage:
    """Google Cloud Storage backend. Any expiry is a property of the bucket's lifecycle
    rule (see DEPLOY.md), never of this code, so `purge_expired` is always a no-op here
    and `IMAGE_RETENTION_DAYS` has no effect. The prod bucket has no rule today, so
    objects are kept forever."""

    def __init__(self, bucket: Optional[str] = None, prefix: Optional[str] = None):
        bucket = bucket or os.environ.get("GCS_BUCKET")
        if not bucket:
            raise ValueError("GCS_BUCKET must be set for the gcs storage backend")
        # Lazy import so local dev needn't install google-cloud-storage.
        from google.cloud import storage  # noqa: F401
        self._client = storage.Client()
        self._bucket = self._client.bucket(bucket)
        self.prefix = (prefix if prefix is not None
                       else os.environ.get("GCS_PREFIX", "")).strip("/")

    def _blob(self, key: str):
        name = f"{self.prefix}/{key}" if self.prefix else key
        return self._bucket.blob(name)

    def put(self, key: str, data: bytes, content_type: Optional[str] = None) -> None:
        self._blob(key).upload_from_string(
            data, content_type=content_type or "application/octet-stream")

    def get(self, key: str) -> Optional[bytes]:
        blob = self._blob(key)
        if not blob.exists():
            return None
        return blob.download_as_bytes()

    def delete(self, key: str) -> None:
        blob = self._blob(key)
        if blob.exists():
            blob.delete()

    def purge_expired(self) -> int:
        return 0  # expiry, if any, belongs to the bucket's lifecycle rule


_storage = None


def get_storage():
    """Process-cached storage backend chosen by IMAGE_STORAGE_BACKEND (default local)."""
    global _storage
    if _storage is None:
        backend = os.environ.get("IMAGE_STORAGE_BACKEND", "local").lower()
        if backend == "gcs":
            _storage = GCSStorage()
        elif backend == "local":
            _storage = LocalStorage()
        else:
            raise ValueError(f"unknown IMAGE_STORAGE_BACKEND: {backend!r}")
    return _storage


def reset_storage() -> None:
    """Drop the cached backend (tests / config changes)."""
    global _storage
    _storage = None
