"""Authentication primitives — password hashing, phone normalization, and opaque
login sessions. Pure logic over a passed-in SQLAlchemy session; no FastAPI here, so
it's reusable by the web app, the admin CLI (scripts/create_user.py), and tests.

No third-party crypto dependency: passwords use the stdlib PBKDF2-HMAC-SHA256, and
sessions are random opaque tokens stored in `user_sessions` (revocable, and shared by
the web cookie and the mobile bearer token).

Mutating helpers flush (so ids are assigned) but DO NOT commit — the caller owns the
transaction and commits once.
"""
import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select

from .db import User, UserSession

ROLES = ("contributor", "telecaller", "admin")
REVIEW_ROLES = ("telecaller", "admin")

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 600_000
_MIN_PASSWORD_LEN = 6


class DuplicatePhone(Exception):
    """Raised when registering a phone that already has an account."""


class DuplicateUsername(Exception):
    """Raised when creating a username that already exists."""


# ── Phone + password ──────────────────────────────────────────────────────────────

def normalize_phone(raw: Optional[str]) -> str:
    """Canonicalize a login phone so '+91 98110 08120' and '9811008120' compare equal.
    Keeps a single leading '+' if present, drops every other non-digit."""
    s = (raw or "").strip()
    if not s:
        return ""
    plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    return ("+" + digits) if plus else digits


def normalize_username(raw: Optional[str]) -> Optional[str]:
    """Usernames are case-insensitive — store and compare lowercased."""
    return (raw or "").strip().lower() or None


def hash_password(password: str) -> str:
    """Return a self-describing 'pbkdf2_sha256$iters$salt_hex$hash_hex' string."""
    if not password:
        raise ValueError("password is required")
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"{_ALGO}${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = (stored or "").split("$")
        if algo != _ALGO:
            return False
        dk = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


# ── Users ─────────────────────────────────────────────────────────────────────────

def find_by_phone(s, phone: str) -> Optional[User]:
    ph = normalize_phone(phone)
    if not ph:
        return None
    return s.execute(select(User).where(User.phone == ph)).scalar_one_or_none()


def find_by_username(s, username: str) -> Optional[User]:
    uname = normalize_username(username)
    if not uname:
        return None
    return s.execute(select(User).where(User.username == uname)).scalar_one_or_none()


def find_by_identifier(s, identifier: str) -> Optional[User]:
    """Resolve a login id that may be a username (operators) or a phone (contributors)."""
    ident = (identifier or "").strip()
    if not ident:
        return None
    return find_by_username(s, ident) or find_by_phone(s, ident)


def create_user(s, password: str, phone: Optional[str] = None,
                username: Optional[str] = None, display_name: Optional[str] = None,
                role: str = "contributor", email: Optional[str] = None,
                registration_source: Optional[str] = None) -> User:
    """Create and flush a new user. Needs at least one login identity (phone for
    contributors, username for operators). Raises DuplicatePhone / DuplicateUsername /
    ValueError on bad input."""
    ph = normalize_phone(phone) or None
    uname = normalize_username(username)
    if not ph and not uname:
        raise ValueError("a phone number or a username is required")
    if not password or len(password) < _MIN_PASSWORD_LEN:
        raise ValueError(f"password must be at least {_MIN_PASSWORD_LEN} characters")
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    if ph and find_by_phone(s, ph):
        raise DuplicatePhone(ph)
    if uname and find_by_username(s, uname):
        raise DuplicateUsername(uname)
    user = User(
        phone=ph,
        username=uname,
        password_hash=hash_password(password),
        display_name=(display_name or "").strip() or None,
        email=(email or "").strip() or None,
        role=role,
        registration_source=registration_source,
    )
    s.add(user)
    s.flush()
    return user


def authenticate(s, identifier: str, password: str) -> Optional[User]:
    """Return the active user for these credentials (identifier = username or phone)."""
    user = find_by_identifier(s, identifier)
    if not user or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


# ── Sessions (web cookie + mobile bearer) ──────────────────────────────────────────

def create_session(s, user: User, ttl_days: int = 30) -> str:
    token = secrets.token_urlsafe(32)
    s.add(UserSession(
        token=token, user_id=user.id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=ttl_days)))
    s.flush()
    return token


def resolve_session(s, token: Optional[str]) -> Optional[User]:
    """Return the active user behind a token, or None if missing/expired/disabled."""
    if not token:
        return None
    row = s.execute(select(UserSession).where(
        UserSession.token == token)).scalar_one_or_none()
    if not row:
        return None
    exp = row.expires_at
    if exp is not None:
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp < datetime.now(timezone.utc):
            return None
    user = s.get(User, row.user_id)
    if not user or not user.is_active:
        return None
    return user


def delete_session(s, token: Optional[str]) -> None:
    if not token:
        return
    row = s.execute(select(UserSession).where(
        UserSession.token == token)).scalar_one_or_none()
    if row:
        s.delete(row)
        s.flush()


# ── Bootstrap ───────────────────────────────────────────────────────────────────────

def ensure_seed_admin(s) -> Optional[User]:
    """Make sure at least one admin exists so a fresh deploy is usable. Identity comes
    from env: ADMIN_USERNAME (default = ADMIN_USER, e.g. 'admin') + ADMIN_PASSWORD. If a
    user with that username already exists it is promoted to admin; otherwise one is
    created. Returns the admin, or None if one already existed."""
    if s.execute(select(User).where(User.role == "admin")).scalar_one_or_none():
        return None
    username = normalize_username(
        os.environ.get("ADMIN_USERNAME") or os.environ.get("ADMIN_USER") or "admin")
    password = os.environ.get("ADMIN_PASSWORD") or "admin"

    existing = find_by_username(s, username)
    if existing:
        existing.role = "admin"
        existing.is_active = True
        s.flush()
        return existing

    admin = User(
        username=username,
        password_hash=hash_password(password),
        display_name="Administrator",
        role="admin",
        registration_source="seed",
    )
    s.add(admin)
    s.flush()
    return admin
