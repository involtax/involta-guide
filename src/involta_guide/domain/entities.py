"""Pure business types. No web, database or provider imports."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class RuleViolation(ValueError):
    pass


class Conflict(RuleViolation):
    pass


class NotFound(RuleViolation):
    pass


class Moderation(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Location:
    id: UUID
    name: str
    slug: str
    kind: str
    parent_id: UUID | None = None
    top_limit: int = 3


@dataclass(frozen=True)
class Restaurant:
    name: str
    cuisine: str
    description: str
    maps_url: str
    phone: str = ""
    id: UUID | None = None
    location_id: UUID | None = None
    alex_star: bool = False


@dataclass(frozen=True)
class ReviewEntry:
    restaurant_id: UUID | None
    text: str | None
    rating: float | None
    sentiment: float | None
    recommendation: float | None


@dataclass(frozen=True)
class Review:
    id: UUID
    location_id: UUID
    moderation: Moderation
    fraud: bool = False


@dataclass(frozen=True)
class Proposal:
    id: UUID
    review_id: UUID
    kind: str
    status: str = "pending"


@dataclass(frozen=True)
class AIJob:
    id: UUID
    kind: str
    owner: UUID
    attempts: int
    lease_until: datetime
    data: dict[str, Any] = field(default_factory=dict)
