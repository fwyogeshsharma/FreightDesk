"""PostgreSQL persistence layer (SQLAlchemy 2.0 + psycopg3).

One `trucks` table holds every sighting from every source (video file, image API,
live stream). `detected_at` is the absolute wall-clock time and is indexed so the
broker app can page through "newest first" cheaply.

Connection string comes from the ``DATABASE_URL`` env var; the default points at a
local Postgres named ``trucks``. Nothing here requires a running server at import
time — the engine connects lazily on first use.
"""
import enum
import os
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, Float, Index, Integer, String, Text,
    create_engine, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

DEFAULT_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/trucks"


def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


class SourceType(str, enum.Enum):
    video = "video"
    image_api = "image_api"
    stream = "stream"


class Base(DeclarativeBase):
    pass


class Truck(Base):
    __tablename__ = "trucks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Absolute time of the sighting — drives the newest-first broker view.
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    source: Mapped[SourceType] = mapped_column(
        Enum(SourceType, name="source_type"), nullable=False, default=SourceType.video)
    source_ref: Mapped[Optional[str]] = mapped_column(String(255))  # video file / batch / stream id

    license_plate: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    plate_confidence: Mapped[Optional[str]] = mapped_column(String(8))  # HIGH / LOW / NONE
    company_name: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    phone_number: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    website: Mapped[Optional[str]] = mapped_column(String(255))
    vehicle_type: Mapped[Optional[str]] = mapped_column(String(32))
    city: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    other_text: Mapped[Optional[str]] = mapped_column(Text)

    frames: Mapped[int] = mapped_column(Integer, default=0)
    first_seen_sec: Mapped[Optional[float]] = mapped_column(Float)
    last_seen_sec: Mapped[Optional[float]] = mapped_column(Float)

    # ── Mobile field-report fields (source = image_api) ──────────────────────────
    loaded_status: Mapped[Optional[str]] = mapped_column(String(16))   # LOADED / UNLOADED
    location: Mapped[Optional[str]] = mapped_column(String(255))       # address / place text
    latitude: Mapped[Optional[float]] = mapped_column(Float)
    longitude: Mapped[Optional[float]] = mapped_column(Float)
    num_wheels: Mapped[Optional[int]] = mapped_column(Integer)
    # Phone provenance: what the reporter typed vs what OCR read; phone_number = merged.
    phone_reported: Mapped[Optional[str]] = mapped_column(String(64))
    phone_ocr: Mapped[Optional[str]] = mapped_column(String(128))
    # Reporter identity. `reported_by` is the human-readable name/label snapshot (works
    # for anonymous submissions too); `reported_by_user_id` links to the users table when
    # the contributor was logged in; `reporter_phone` snapshots the reporter's own account
    # phone at submission time (distinct from phone_reported, which is the TRUCK's phone).
    reported_by: Mapped[Optional[str]] = mapped_column(String(128))
    reported_by_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, index=True)
    reporter_phone: Mapped[Optional[str]] = mapped_column(String(32))
    # Trust gate for paid mobile reports: VERIFIED (photos confirm the plate) vs
    # UNVERIFIED (could not confirm — not auto-trusted/paid). NULL for video/stream.
    verification_status: Mapped[Optional[str]] = mapped_column(String(16), index=True)
    # Telecaller/admin decision. PASSED = contributor is reward-eligible.
    review_status: Mapped[Optional[str]] = mapped_column(String(16), index=True)  # PENDING/PASSED/REJECTED
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(128))
    reviewed_by_user_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[Optional[str]] = mapped_column(String(500))

    # Full audit data for the detail view.
    plate_candidates: Mapped[Optional[dict]] = mapped_column(JSONB)
    body_texts: Mapped[Optional[list]] = mapped_column(JSONB)

    image_path: Mapped[Optional[str]] = mapped_column(String(255))  # representative crop, relative to output/

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())

    # Newest-first paging is the hot query — index detected_at descending.
    __table_args__ = (
        Index("ix_trucks_detected_at_desc", detected_at.desc()),
    )

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "detected_at": self.detected_at.isoformat() if self.detected_at else None,
            "source": self.source.value if isinstance(self.source, SourceType) else self.source,
            "source_ref": self.source_ref,
            "license_plate": self.license_plate,
            "plate_confidence": self.plate_confidence,
            "company_name": self.company_name,
            "phone_number": self.phone_number,
            "website": self.website,
            "vehicle_type": self.vehicle_type,
            "city": self.city,
            "other_text": self.other_text,
            "frames": self.frames,
            "first_seen_sec": self.first_seen_sec,
            "last_seen_sec": self.last_seen_sec,
            "loaded_status": self.loaded_status,
            "location": self.location,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "num_wheels": self.num_wheels,
            "phone_reported": self.phone_reported,
            "phone_ocr": self.phone_ocr,
            "reported_by": self.reported_by,
            "reported_by_user_id": self.reported_by_user_id,
            "reporter_phone": self.reporter_phone,
            "verification_status": self.verification_status,
            "review_status": self.review_status,
            "reviewed_by": self.reviewed_by,
            "reviewed_by_user_id": self.reviewed_by_user_id,
            "reviewed_at": self.reviewed_at.isoformat() if self.reviewed_at else None,
            "review_note": self.review_note,
            "plate_candidates": self.plate_candidates,
            "body_texts": self.body_texts,
            "image_path": self.image_path,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class SubmissionLog(Base):
    """One row per mobile /report call — the abuse-tracking audit trail.

    Records every submission (verified or not) with the contributor id and the
    reason, so repeat bad actors farming rewards with junk uploads are visible.
    """
    __tablename__ = "submission_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True)
    reported_by: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    reported_by_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, index=True)
    reporter_phone: Mapped[Optional[str]] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))           # VERIFIED / UNVERIFIED
    reason: Mapped[Optional[str]] = mapped_column(String(255))
    vehicle_reported: Mapped[Optional[str]] = mapped_column(String(32))
    vehicle_ocr: Mapped[Optional[str]] = mapped_column(String(32))
    phone_reported: Mapped[Optional[str]] = mapped_column(String(64))
    phone_ocr: Mapped[Optional[str]] = mapped_column(String(128))
    images_count: Mapped[Optional[int]] = mapped_column(Integer)
    truck_id: Mapped[Optional[int]] = mapped_column(BigInteger)  # the stored trucks.id

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "reported_by": self.reported_by,
            "reported_by_user_id": self.reported_by_user_id,
            "reporter_phone": self.reporter_phone,
            "status": self.status,
            "reason": self.reason,
            "vehicle_reported": self.vehicle_reported,
            "vehicle_ocr": self.vehicle_ocr,
            "phone_reported": self.phone_reported,
            "phone_ocr": self.phone_ocr,
            "images_count": self.images_count,
            "truck_id": self.truck_id,
        }


class User(Base):
    """A person who authenticates with FreightDesk. One unified table for both
    external mobile contributors (self-register, role 'contributor') and internal
    operators (created by an admin via scripts/create_user.py — 'telecaller'/'admin')."""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # Login identity. Contributors (mobile) log in by phone; internal operators
    # (telecaller/admin) log in by username. Both are unique and both nullable, so a
    # user carries only the identifier that fits their role (operator phone is optional).
    phone: Mapped[Optional[str]] = mapped_column(String(32), unique=True, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[Optional[str]] = mapped_column(String(255))  # optional contact
    display_name: Mapped[Optional[str]] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="contributor")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    registration_source: Mapped[Optional[str]] = mapped_column(String(32))  # mobile / cli / seed
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())

    def as_dict(self) -> dict:
        """Public-safe shape — never includes the password hash."""
        return {
            "id": self.id,
            "phone": self.phone,
            "username": self.username,
            "email": self.email,
            "display_name": self.display_name,
            "role": self.role,
            "is_active": self.is_active,
            "registration_source": self.registration_source,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class UserSession(Base):
    """An opaque, revocable login token. Backs both the web session cookie and the
    mobile bearer token — same table, one auth model."""
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ── Engine / session factory ────────────────────────────────────────────────────

_engine = None
_SessionLocal = None


def get_engine():
    """Lazily build a process-local engine. Each OS process (incl. spawn workers)
    gets its own — engines must never be shared across a fork/spawn boundary."""
    global _engine
    if _engine is None:
        _engine = create_engine(database_url(), pool_pre_ping=True, future=True)
    return _engine


def get_session_factory() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionLocal


def reset_engine():
    """Drop the cached engine/session factory. Call this at the start of a spawned
    worker so it builds its own connection rather than inheriting the parent's."""
    global _engine, _SessionLocal
    _engine = None
    _SessionLocal = None


def init_db():
    """Create the trucks table and indexes if they don't exist."""
    Base.metadata.create_all(get_engine())
