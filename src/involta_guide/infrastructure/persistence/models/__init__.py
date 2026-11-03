"""Persistence mappings; existing PostgreSQL names are intentionally retained."""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, SmallInteger, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class LocationRow(Base):
    __tablename__ = "locations"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    region: Mapped[str] = mapped_column(Text)
    slug: Mapped[str] = mapped_column(Text)
    population: Mapped[int | None] = mapped_column(Integer)
    active: Mapped[int] = mapped_column(Integer)
    intro: Mapped[str] = mapped_column(Text)
    draft: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    published_version: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    draft_version: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    kind: Mapped[str] = mapped_column(Text)
    parent_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    is_hub: Mapped[bool] = mapped_column(Boolean)
    in_phrase: Mapped[str] = mapped_column(Text)
    aliases: Mapped[list[str]] = mapped_column(ARRAY(Text))
    priority: Mapped[str] = mapped_column(Text)
    top_limit: Mapped[int] = mapped_column(SmallInteger)
    population_verified: Mapped[bool] = mapped_column(Boolean)
    population_basis: Mapped[str] = mapped_column(Text)
    source_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB)
    coverage_note: Mapped[str] = mapped_column(Text)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    bounds: Mapped[dict[str, float] | None] = mapped_column(JSONB)
    listed: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RestaurantRow(Base):
    __tablename__ = "restaurants"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    location_id: Mapped[str] = mapped_column(UUID(as_uuid=False))
    name: Mapped[str] = mapped_column(Text)
    cuisine: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    maps_url: Mapped[str] = mapped_column(Text)
    sources: Mapped[list[str]] = mapped_column(JSONB)
    active: Mapped[int] = mapped_column(Integer)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    phone: Mapped[str] = mapped_column(Text)
    opening_hours: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    alex_star: Mapped[bool] = mapped_column(Boolean)


class VersionRow(Base):
    __tablename__ = "versions"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    location_id: Mapped[str] = mapped_column(UUID(as_uuid=False))
    number: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    prompt: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    published: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReviewRow(Base):
    __tablename__ = "reviews"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    location_id: Mapped[str] = mapped_column(UUID(as_uuid=False))
    visitor: Mapped[str] = mapped_column(Text)
    audio: Mapped[str] = mapped_column(Text)
    mime: Mapped[str] = mapped_column(Text)
    hash: Mapped[str] = mapped_column(Text)
    ip_hash: Mapped[str] = mapped_column(Text)
    device: Mapped[str] = mapped_column(Text)
    transcript: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text)
    moderation: Mapped[str] = mapped_column(Text)
    fraud: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    alex_session_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    admin_session_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProposalRow(Base):
    __tablename__ = "proposals"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    location_id: Mapped[str] = mapped_column(UUID(as_uuid=False))
    review_id: Mapped[str] = mapped_column(UUID(as_uuid=False))
    kind: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReviewTextRow(Base):
    __tablename__ = "review_texts"
    review_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    original_text: Mapped[str] = mapped_column(Text)
    editorial_text: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer)
    restaurant_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    public: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class VoiceLinkRow(Base):
    __tablename__ = "voice_links"
    review_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    reference: Mapped[str] = mapped_column(UUID(as_uuid=False))
    location_id: Mapped[str] = mapped_column(UUID(as_uuid=False))
    number: Mapped[int] = mapped_column(BigInteger)
    active_run: Mapped[int] = mapped_column(Integer)
    context_snapshot: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB)


class VoiceProposalRow(Base):
    __tablename__ = "voice_proposals"
    proposal_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    review_id: Mapped[str] = mapped_column(UUID(as_uuid=False))
    run: Mapped[int] = mapped_column(Integer)
    number: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer)


class JobRow(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    location_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    review_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    prompt: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    applied: Mapped[bool] = mapped_column(Boolean)


class RequestLogRow(Base):
    __tablename__ = "request_logs"
    location_slug: Mapped[str] = mapped_column(Text, primary_key=True)
    number: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_id: Mapped[str] = mapped_column(UUID(as_uuid=False))
    path: Mapped[str] = mapped_column(Text)


class AuditRow(Base):
    __tablename__ = "audit"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    action: Mapped[str] = mapped_column(Text)
    target: Mapped[str] = mapped_column(Text)
    detail: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SettingRow(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class LoginAttemptRow(Base):
    __tablename__ = "login_attempts"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    count: Mapped[int] = mapped_column(Integer)
    reset_at: Mapped[int] = mapped_column(BigInteger)


class VersionReviewRow(Base):
    __tablename__ = "version_reviews"
    version_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    review_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)


class RestaurantLocationRow(Base):
    __tablename__ = "restaurant_locations"
    restaurant_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    location_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)


class LocationRelationRow(Base):
    __tablename__ = "location_relations"
    location_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    related_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)


class LocationRedirectRow(Base):
    __tablename__ = "location_slug_redirects"
    slug: Mapped[str] = mapped_column(Text, primary_key=True)
    location_id: Mapped[str] = mapped_column(UUID(as_uuid=False))


class AlexSessionRow(Base):
    __tablename__ = "alex_sessions"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    token_hash: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AlexChangeRow(Base):
    __tablename__ = "alex_changes"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    session_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    admin_session_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    review_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    location_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    version_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    before_content: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after_content: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text)
    detail: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AdminSessionRow(Base):
    __tablename__ = "admin_sessions"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    token_hash: Mapped[str] = mapped_column(Text)
    csrf_hash: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AIUsageRow(Base):
    __tablename__ = "ai_usage"
    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    job_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False))
    model: Mapped[str] = mapped_column(Text)
    operation: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float)
    detail: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
