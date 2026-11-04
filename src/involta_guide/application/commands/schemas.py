from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from involta_guide.domain.rules import FACTORS, KINDS


class Hours(BaseModel):
    timezone: str
    week: list[list[tuple[str, str]]] = Field(min_length=7, max_length=7)
    exceptions: dict[str, list[tuple[str, str]]] = Field(default_factory=dict)

    @field_validator("week", "exceptions", mode="before")
    @classmethod
    def intervals(cls, value: Any) -> Any:
        # Both representations describe the same interval; storage remains [start, end].
        def convert(item: Any) -> Any:
            if isinstance(item, dict):
                if set(item) == {"start", "end"}:
                    return [item["start"], item["end"]]
                return {key: convert(val) for key, val in item.items()}
            if isinstance(item, list):
                return [convert(val) for val in item]
            return item

        return convert(value)

    @field_validator("timezone")
    @classmethod
    def zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Неизвестный часовой пояс") from exc
        return value

    @model_validator(mode="after")
    def clock(self) -> "Hours":
        import re
        from datetime import date

        for day, slots in self.exceptions.items():
            date.fromisoformat(day)
        for slots in [*self.week, *self.exceptions.values()]:
            if len(slots) > 4:
                raise ValueError("Не более 4 интервалов в день")
            for slot in slots:
                if any(not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", clock) for clock in slot):
                    raise ValueError("Время должно быть HH:MM")
        return self


class RestaurantInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID | None = None
    locationId: UUID | None = None
    locationName: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    name: str = Field(min_length=1, max_length=200)
    cuisine: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=2000)
    mapsUrl: str = Field(max_length=2000)
    sources: list[str] = Field(default_factory=list, max_length=50)
    phone: str = ""
    openingHours: Hours | None = None
    alexStar: bool = False

    @field_validator("mapsUrl")
    @classmethod
    def maps(cls, value: str) -> str:
        from urllib.parse import urlsplit

        url = urlsplit(value)
        if (
            url.scheme != "https"
            or url.username
            or url.password
            or not (
                url.hostname in ("yandex.ru", "yandex.com", "yandex.by", "yandex.kz", "ya.ru")
                or (url.hostname or "").endswith(".yandex.ru")
            )
        ):
            raise ValueError("Нужна HTTPS-ссылка на Яндекс Карты")
        return value

    @field_validator("sources")
    @classmethod
    def sources_valid(cls, value: list[str]) -> list[str]:
        from urllib.parse import urlsplit

        for source in value:
            url = urlsplit(source)
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                raise ValueError("Нужна HTTPS-ссылка источника")
        return value

    @field_validator("phone")
    @classmethod
    def phone_valid(cls, value: str) -> str:
        import re

        if value and not re.fullmatch(r"\+[1-9]\d{7,14}", value):
            raise ValueError("Телефон: + и 8–15 цифр")
        return value


class RankingCandidate(BaseModel):
    name: str
    mapsUrl: str | None
    scores: dict[str, StrictInt | None]
    evidence: list[dict[str, Any]] = Field(min_length=8, max_length=8)
    totalScore: float | None = None

    @field_validator("mapsUrl")
    @classmethod
    def maps_valid(cls, value: str | None) -> str | None:
        return RestaurantInput.maps(value) if value is not None else None

    @model_validator(mode="after")
    def factors(self) -> "RankingCandidate":
        if set(self.scores) != set(FACTORS) or any(
            value is not None and not 0 <= value <= 10 for value in self.scores.values()
        ):
            raise ValueError("Нужны восемь оценок от 0 до 10 или null")
        if {e.get("factor") for e in self.evidence} != set(FACTORS):
            raise ValueError("Нужны обоснования восьми факторов")
        return self


class CandidateReview(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    mapsUrl: str | None = None
    status: Literal["verified", "excluded", "needs_verification"]
    reason: str = Field(min_length=1, max_length=2000)
    exclusionCode: Literal["outside_area", "closed", "not_restaurant", "duplicate"] | None = None
    sources: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def decision(self) -> "CandidateReview":
        if not self.reason.strip():
            raise ValueError("Укажите конкретную причину решения")
        if self.status == "excluded" and not self.exclusionCode:
            raise ValueError(
                "Исключение требует причины: outside_area, closed, not_restaurant или duplicate"
            )
        if self.status != "excluded" and self.exclusionCode is not None:
            raise ValueError("exclusionCode применим только к исключённому кандидату")
        return self

    @field_validator("mapsUrl")
    @classmethod
    def maps_valid(cls, value: str | None) -> str | None:
        return RestaurantInput.maps(value) if value is not None else None

    @field_validator("sources")
    @classmethod
    def sources_valid(cls, value: list[str]) -> list[str]:
        return RestaurantInput.sources_valid(value)


class ContentInput(BaseModel):
    intro: str = Field(max_length=4000)
    restaurants: list[RestaurantInput] = Field(max_length=50)
    rankingReport: list[RankingCandidate] | None = Field(default=None, max_length=10)
    candidateReview: list[CandidateReview] | None = Field(default=None, max_length=100)
    qualityReport: dict[str, Any] | None = None


class GeneratedContent(ContentInput):
    rankingReport: list[RankingCandidate] = Field(max_length=10)
    warnings: list[str] = Field(default_factory=list, max_length=20)


class GenerationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    factor: str
    reason: str = Field(min_length=1, max_length=300)
    confidence: Literal["high", "medium", "low"]
    sourceRefs: list[int] = Field(default_factory=list, max_length=10)


class GenerationCandidate(CandidateReview):
    """Research facts only; persistence IDs and administrative flags never enter this schema."""

    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=500)
    cuisine: str = Field(default="", max_length=300)
    description: str = Field(default="", max_length=800)
    phone: str = ""
    openingHours: Hours | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    scores: dict[str, StrictInt | None] = Field(default_factory=dict)
    evidence: list[GenerationEvidence] = Field(default_factory=list, max_length=8)

    @field_validator("phone")
    @classmethod
    def valid_phone(cls, value: str) -> str:
        return RestaurantInput.phone_valid(value)

    @model_validator(mode="after")
    def assessment(self) -> "GenerationCandidate":
        if self.scores:
            if set(self.scores) != set(FACTORS) or any(
                value is not None and not 0 <= value <= 10 for value in self.scores.values()
            ):
                raise ValueError("Нужны восемь оценок от 0 до 10 или null")
            if len(self.evidence) != 8 or {e.factor for e in self.evidence} != set(FACTORS):
                raise ValueError("Нужны краткие обоснования восьми факторов")
        elif self.evidence:
            raise ValueError("Обоснования требуют оценки факторов")
        for item in self.evidence:
            if len(set(item.sourceRefs)) != len(item.sourceRefs) or any(
                index < 1 or index > len(self.sources) for index in item.sourceRefs
            ):
                raise ValueError("sourceRefs должны ссылаться на номера sources начиная с 1")
        if self.status == "verified" and (
            not self.mapsUrl
            or not self.phone
            or not self.cuisine.strip()
            or not self.description.strip()
            or set(self.scores) != set(FACTORS)
            or any(value is None for value in self.scores.values())
        ):
            raise ValueError("verified требует карточку, телефон и восемь полных оценок")
        if self.status == "excluded" and (self.scores or self.evidence):
            raise ValueError("Исключённому кандидату не нужны оценки")
        return self


class GenerationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intro: str = Field(max_length=800)
    candidates: list[GenerationCandidate] = Field(max_length=100)
    warnings: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def candidates_valid(self) -> "GenerationResult":
        names = [" ".join(c.name.casefold().split()) for c in self.candidates]
        if len(names) != len(set(names)):
            raise ValueError("Кандидат повторяется")
        if sum(bool(c.scores) for c in self.candidates) > 10:
            raise ValueError("Оценивай не более десяти кандидатов")
        return self


class LocationInput(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    region: str = Field(min_length=1, max_length=200)
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=200)
    kind: str = "city"
    parent_id: UUID | None = None
    is_hub: bool = False
    in_phrase: str = Field(min_length=3, max_length=200)
    aliases: list[str] = Field(default_factory=list, max_length=30)
    priority: Literal["high", "medium", "low"] = "medium"
    top_limit: int = Field(default=3, ge=1, le=10)
    active: int = Field(default=1, ge=0, le=1)
    population: int | None = Field(default=None, ge=0, le=100000000)
    population_verified: bool = False
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    coverage_note: str = Field(default="", max_length=3000)
    bounds: dict[str, float] | None = None
    listed: bool = True
    related_ids: list[UUID] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def bounds_valid(self) -> "LocationInput":
        if self.kind not in KINDS:
            raise ValueError("Неизвестный тип локации")
        if self.bounds:
            b = self.bounds
            if (
                set(b) != {"west", "east", "south", "north"}
                or not -180 <= b["west"] < b["east"] <= 180
                or not -90 <= b["south"] < b["north"] <= 90
            ):
                raise ValueError("Некорректные границы")
        return self
