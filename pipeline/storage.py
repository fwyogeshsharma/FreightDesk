"""Temporary photo storage for mobile field reports.

Photos are kept only long enough to OCR them and let a telecaller eyeball them
during review — **retained at most `IMAGE_RETENTION_DAYS` (default 2), then deleted.**
Only the extracted text in the `trucks` table is permanent.

Two interchangeable backends, selected by `IMAGE_STORAGE_BACKEND`:

- ``local`` (dev) — files under a base dir; a sweeper deletes anything older than the
  retention window. Env: ``IMAGE_STORAGE_DIR`` (default ``<root>/uploads``).
- ``gcs`` (prod) — a Google Cloud Storage bucket whose **lifecycle rule** auto-deletes
  objects after the retention window, so expiry is enforced by GCP, not by this code.
  Env: ``GCS_BUCKET`` (required), ``GCS_PREFIX`` (optional key prefix).

Both implement the same tiny interface: ``put(key, data, content_type)``,
``get(key) -> bytes|None``, ``delete(key)``, ``purge_expired()``. Keys are opaque
strings like ``reports/<truck_id>/<idx>.jpg``.
"""
import os
import time
from pathlib import Path
from typing import Optional

# Project root (folder containing pipeline/).
_ROOT = Path(__file__).resolve().parent.parent

RETENTION_DAYS = float(os.environ.get("IMAGE_RETENTION_DAYS", "2"))
RETENTION_SECONDS = RETENTION_DAYS * 86400


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
        """Delete files older than the retention window. Returns count removed."""
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
    """Google Cloud Storage backend. Expiry is handled by the bucket's lifecycle
    rule (see DEPLOY.md), so `purge_expired` is a no-op here."""

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
        return 0  # bucket lifecycle rule deletes objects after the retention window


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
