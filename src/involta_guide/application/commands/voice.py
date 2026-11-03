"""Versioned whole-review package decisions shared by manual and future automatic moderation."""

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: UUID
    review_revision: int = Field(ge=1)
    processing_run: int = Field(ge=0)
    action: str
    reason: str = Field(default="", max_length=2000)
    text: str | None = Field(default=None, max_length=30000)
    restaurant_id: UUID | None = None
    proposal_id: UUID | None = None
    proposal_revision: int | None = None
    page_revision: UUID | None = None
    changes: dict[str, Any] = Field(default_factory=dict)
    proposal_edits: dict[str, dict[str, Any]] = Field(default_factory=dict)
    excluded_proposals: list[UUID] = Field(default_factory=list)
    affected_revisions: dict[str, str | None] = Field(default_factory=dict)
    run: int | None = None
    fragment_number: int | None = Field(default=None, ge=0)
