from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from involta_guide.domain.rules import FACTORS, KINDS


class Hours(BaseModel):
    timezone: str
    week: list[list[tuple[str, str]]] = Field(min_length=7, max_length=7)
    exceptions: dict[str, list[tuple[str, str]]] = Field(default_factory=dict)

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
    sources: list[str] = Field(default_factory=list, max_length=20)
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


class ContentInput(BaseModel):
    intro: str = Field(max_length=4000)
    restaurants: list[RestaurantInput] = Field(max_length=50)
    rankingReport: list[RankingCandidate] | None = Field(default=None, max_length=10)


class Generation(ContentInput):
    rankingReport: list[RankingCandidate] = Field(max_length=10)
    warnings: list[str] = Field(default_factory=list, max_length=20)


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


class ReviewEntryInput(BaseModel):
    restaurantId: UUID | None
    restaurantName: str
    type: Literal["review", "rating"]
    text: str | None
    quote: str
    sentiment: float | None = Field(ge=-10, le=10)
    recommendation: float | None = Field(ge=-10, le=10)
    rating: float | None = Field(ge=0, le=10)
    confidence: float = Field(ge=0, le=1)
    topics: list[Literal["food", "service", "atmosphere", "cleanliness", "price", "facts"]]
    experience: Literal["personal", "hearsay", "unknown"]

    @model_validator(mode="after")
    def meaningful(self) -> "ReviewEntryInput":
        if self.type == "review" and not (self.text or "").strip():
            raise ValueError("Текст отзыва обязателен")
        if self.type == "rating" and all(
            value is None for value in (self.sentiment, self.recommendation, self.rating)
        ):
            raise ValueError("Оценка без показателей")
        return self


class Suggestion(BaseModel):
    kind: Literal["edit", "restaurant"]
    restaurantId: UUID | None
    name: str = ""
    cuisine: str = ""
    description: str = ""
    mapsUrl: str = ""
    phone: str | None = None
    openingHours: Hours | None = None
    reason: str


class Analysis(BaseModel):
    entries: list[ReviewEntryInput] = Field(min_length=1, max_length=20)
    suggestions: list[Suggestion] = Field(max_length=20)


class AlexAction(BaseModel):
    kind: Literal["add", "update", "remove"]
    restaurantId: UUID | None
    changes: dict[str, Any]

    @field_validator("changes")
    @classmethod
    def only_card_fields(cls, value: dict[str, Any]) -> dict[str, Any]:
        allowed = set(RestaurantInput.model_fields) - {"id", "locationId", "locationName"}
        if set(value) - allowed:
            raise ValueError("Неизвестное поле команды")
        return value


class AlexCommands(BaseModel):
    intro: str | None = Field(default=None, max_length=4000)
    comment: str | None = Field(default=None, max_length=4000)
    actions: list[AlexAction] = Field(max_length=20)
