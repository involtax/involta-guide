"""Versioned boundary DTOs shared as data contracts, never runtime coupling."""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, model_validator


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RestaurantContext(DTO):
    id: UUID
    name: str
    phone: str = ""
    description: str = ""
    openingHours: dict[str, Any] | None = None


class LocationContext(DTO):
    id: UUID
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=200)
    name: str
    revision: str | None = None
    published_revision: str | None = None
    restaurants: list[RestaurantContext] = Field(default_factory=list, max_length=200)


class SubmissionMetadata(DTO):
    schema_version: Literal[1] = 1
    location: LocationContext
    visitor: str = Field(min_length=32, max_length=200)
    idempotency_key: UUID
    ip_fingerprint: str = Field(min_length=32, max_length=200)
    user_agent: str = Field(default="", max_length=500)
    widget_version: Literal["1"] = "1"
    language: str = Field(default="", max_length=40)
    timezone: str = Field(default="", max_length=100)
    actor: Literal["visitor"] = "visitor"
    correlation_id: UUID


class ReviewFragment(DTO):
    restaurantId: UUID | None = None
    restaurantName: str = ""
    candidates: list[UUID] = Field(default_factory=list, max_length=10)
    quote: str = Field(min_length=1, max_length=20000)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    type: Literal["review", "rating"] = "review"
    text: str | None = None
    sentiment: StrictInt | StrictFloat | None = Field(default=None, ge=-10, le=10)
    recommendation: StrictInt | StrictFloat | None = Field(default=None, ge=-10, le=10)
    rating: StrictInt | StrictFloat | None = Field(default=None, ge=0, le=10)
    confidence: float = Field(default=0, ge=0, le=1)
    topics: list[str] = Field(default_factory=list, max_length=20)
    experience: Literal["personal", "hearsay", "unknown"] = "unknown"


class Suggestion(DTO):
    kind: Literal["edit", "restaurant"]
    restaurantId: UUID | None = None
    name: str = ""
    quote: str = Field(min_length=1)
    reason: str
    changes: dict[str, Any] = Field(default_factory=dict)


class AnalysisResult(DTO):
    outcome: Literal["review", "no_action"]
    reason: str = ""
    entries: list[ReviewFragment] = Field(default_factory=list, max_length=20)
    suggestions: list[Suggestion] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def no_action(self) -> "AnalysisResult":
        if self.outcome == "no_action" and (self.entries or self.suggestions or not self.reason):
            raise ValueError("no_action requires a reason and no attributed experience/actions")
        return self


def validate_analysis(value: Any, transcript: str, context: LocationContext) -> AnalysisResult:
    result = AnalysisResult.model_validate(value)
    known = {r.id for r in context.restaurants}
    ranges: list[tuple[int, int]] = []
    for entry in result.entries:
        if transcript[entry.start : entry.end] != entry.quote:
            raise ValueError("Quote must match the exact transcript fragment")
        if entry.restaurantId is not None and entry.restaurantId not in known:
            raise ValueError("Restaurant does not belong to the submitted context")
        if any(candidate not in known for candidate in entry.candidates):
            raise ValueError("Unknown candidate restaurant")
        if any(entry.start < end and start < entry.end for start, end in ranges):
            raise ValueError("Overlapping fragments would duplicate the same experience")
        ranges.append((entry.start, entry.end))
    for suggestion in result.suggestions:
        if suggestion.quote not in transcript:
            raise ValueError("Suggestion quote is absent from transcript")
        if suggestion.restaurantId is not None and suggestion.restaurantId not in known:
            raise ValueError("Unknown suggestion restaurant")
        if set(suggestion.changes) - {
            "name",
            "cuisine",
            "description",
            "mapsUrl",
            "phone",
            "openingHours",
        }:
            raise ValueError("Forbidden suggestion fields")
    return result
