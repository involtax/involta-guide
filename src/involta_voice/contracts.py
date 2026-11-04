"""Versioned boundary DTOs shared as data contracts, never runtime coupling."""

import re
from datetime import date
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, model_validator


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LocationReset(DTO):
    content_epoch: StrictInt = Field(ge=1)


class LocationPurged(DTO):
    purged: Literal[True]
    content_epoch: StrictInt = Field(ge=1)


class OpeningHours(DTO):
    timezone: str
    week: list[list[tuple[str, str]]] = Field(min_length=7, max_length=7)
    exceptions: dict[str, list[tuple[str, str]]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def clocks(self) -> "OpeningHours":
        try:
            ZoneInfo(self.timezone)
        except (KeyError, ValueError) as exc:
            raise ValueError("Unknown timezone") from exc
        for value in self.exceptions:
            date.fromisoformat(value)
        for intervals in [*self.week, *self.exceptions.values()]:
            if len(intervals) > 4:
                raise ValueError("Too many daily intervals")
            if any(
                not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clock)
                for pair in intervals
                for clock in pair
            ):
                raise ValueError("Invalid clock")
        return self


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
    content_epoch: int = Field(ge=1)
    restaurants: list[RestaurantContext] = Field(default_factory=list, max_length=200)


class SubmissionMetadata(DTO):
    schema_version: Literal[2] = 2
    location: LocationContext
    visitor: str = Field(min_length=32, max_length=200)
    idempotency_key: UUID
    ip: str = Field(min_length=1, max_length=200)
    user_agent: str = Field(default="", max_length=10000)
    widget_version: Literal["1"] = "1"
    language: str = Field(default="", max_length=40)
    timezone: str = Field(default="", max_length=100)
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


class SubmissionStatus(DTO):
    reference: UUID
    location: LocationContext
    state: Literal[
        "accepted",
        "transcription",
        "analysis",
        "result_ready",
        "delivered_to_guide",
        "transcription_failed",
        "transcription_unknown",
        "analysis_failed",
        "analysis_unknown",
    ]
    run: int = Field(ge=1)
    transcript: str | None
    created_at: str
    error: str | None = None
    repeat_signals: dict[str, int] = Field(default_factory=dict)


class OutboxEvent(DTO):
    schema_version: Literal[2]
    event_id: UUID
    sequence: int = Field(ge=1)
    kind: Literal[
        "recording_received",
        "gigaam_ready",
        "transcript_ready",
        "analysis_ready",
        "processing_failed",
    ]
    reference: UUID
    run: int = Field(ge=1)
    metadata: SubmissionMetadata
    transcript: str | None = Field(default=None, min_length=1, max_length=30000)
    analysis: dict[str, Any] | None = None
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime: Literal["audio/webm", "audio/ogg", "audio/mp4", "audio/wav"]
    duration: float = Field(ge=1, le=121)
    created_at: str
    repeat_signals: dict[str, int] = Field(default_factory=dict)


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
        if entry.rating is not None:
            number = str(entry.rating).removesuffix(".0")
            numeric = re.search(
                r"(?<!\d)" + re.escape(number).replace(r"\.", r"[.,]") + r"(?!\d)", entry.quote
            )
            words = {
                0: "нул",
                1: "один|единиц",
                2: "два|двойк",
                3: "три|тройк",
                4: "четыр|четвер",
                5: "пять|пятер",
                6: "шесть|шестер",
                7: "семь|семер",
                8: "восем|восьмер",
                9: "девят",
                10: "десят",
            }
            verbal = entry.rating in words and re.search(
                r"\b(?:" + words[int(entry.rating)] + r")\w*", entry.quote, re.I
            )
            explicit = re.search(r"оцен|балл|ставлю|из\s*(?:10|десят)|/\s*10", entry.quote, re.I)
            if not (numeric or verbal) or not explicit:
                raise ValueError("Rating must be explicitly stated in the quoted speech")
        if entry.type == "rating" and entry.text is not None:
            raise ValueError("Numeric attributes must not duplicate the review with another text")
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
        for name, value in suggestion.changes.items():
            if name == "openingHours":
                if value is not None:
                    suggestion.changes[name] = OpeningHours.model_validate(value).model_dump(
                        mode="json"
                    )
            elif value is not None and (not isinstance(value, str) or len(value) > 4000):
                raise ValueError("Invalid suggested text value")
            elif name == "phone" and value and not re.fullmatch(r"\+[1-9]\d{7,14}", value):
                raise ValueError("Invalid suggested phone")
            elif name == "mapsUrl" and value:
                parsed = urlsplit(value)
                if (
                    parsed.scheme != "https"
                    or parsed.username
                    or parsed.password
                    or not (
                        parsed.hostname
                        in ("yandex.ru", "yandex.com", "yandex.by", "yandex.kz", "ya.ru")
                        or (parsed.hostname or "").endswith(".yandex.ru")
                    )
                ):
                    raise ValueError("Invalid suggested maps URL")
    return result
