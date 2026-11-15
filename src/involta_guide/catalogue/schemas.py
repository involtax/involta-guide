from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from involta_guide.application.commands.schemas import Hours, RestaurantInput
from involta_guide.application.ports.repositories import MapResolver
from involta_guide.domain.rules import FACTORS

Operation = Literal["intro", "discover", "research"]
Scope = Literal["all", "card", "assessment"]


class StrictResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @classmethod
    def response_schema(cls) -> dict[str, Any]:
        schema = cls.model_json_schema()

        def strict(item: Any) -> None:
            if isinstance(item, dict):
                item.pop("default", None)
                if item.get("title") == "Scores":
                    item.clear()
                    item.update(
                        type="object",
                        properties={
                            key: {
                                "anyOf": [
                                    {"type": "integer", "minimum": 0, "maximum": 10},
                                    {"type": "null"},
                                ]
                            }
                            for key in FACTORS
                        },
                    )
                if "properties" in item:
                    item["required"] = list(item["properties"])
                    item["additionalProperties"] = False
                for value in item.values():
                    strict(value)
            elif isinstance(item, list):
                for value in item:
                    strict(value)

        strict(schema)
        return schema


class Introduction(StrictResult):
    intro: str = Field(min_length=1, max_length=30000)

    @field_validator("intro")
    @classmethod
    def meaningful(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("Вступление не должно быть пустым")
        return value.strip()


class Discovery(StrictResult):
    name: str = Field(min_length=1, max_length=200)
    mapsUrl: str | None = None
    sources: list[str] = Field(min_length=1, max_length=50)
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("Название не должно быть пустым")
        return " ".join(value.split())

    @field_validator("mapsUrl")
    @classmethod
    def map_valid(cls, value: str | None) -> str | None:
        return RestaurantInput.maps(value) if value else None

    @field_validator("sources")
    @classmethod
    def sources_valid(cls, value: list[str]) -> list[str]:
        return RestaurantInput.sources_valid(value)


class Discoveries(StrictResult):
    restaurants: list[Discovery] = Field(max_length=200)
    warnings: list[str] = Field(default_factory=list, max_length=50)


class Card(StrictResult):
    name: str = Field(min_length=1, max_length=200)
    cuisine: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=2000)
    mapsUrl: str | None = None
    phone: str | None = None
    openingHours: Hours | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    sources: list[str] = Field(min_length=1, max_length=50)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return Discovery.clean_name(value)

    @field_validator("mapsUrl")
    @classmethod
    def map_valid(cls, value: str | None) -> str | None:
        return RestaurantInput.maps(value) if value else None

    @field_validator("phone")
    @classmethod
    def phone_valid(cls, value: str | None) -> str | None:
        return RestaurantInput.phone_valid(value) if value else None

    @field_validator("sources")
    @classmethod
    def sources_valid(cls, value: list[str]) -> list[str]:
        return RestaurantInput.sources_valid(value)


class Evidence(StrictResult):
    factor: Literal[
        "food",
        "consistency",
        "identity",
        "localConnection",
        "atmosphere",
        "service",
        "value",
        "destination",
    ]
    reason: str = Field(min_length=1, max_length=2000)
    confidence: Literal["high", "medium", "low"]
    sources: list[str] = Field(min_length=1, max_length=50)

    @field_validator("sources")
    @classmethod
    def sources_valid(cls, value: list[str]) -> list[str]:
        return RestaurantInput.sources_valid(value)


class Assessment(StrictResult):
    scores: dict[str, StrictInt | None]
    evidence: list[Evidence] = Field(min_length=8, max_length=8)

    @model_validator(mode="after")
    def valid_factors(self) -> "Assessment":
        if set(self.scores) != set(FACTORS) or any(
            v is not None and not 0 <= v <= 10 for v in self.scores.values()
        ):
            raise ValueError("Нужны восемь оценок 0–10 или null")
        if {e.factor for e in self.evidence} != set(FACTORS):
            raise ValueError("Нужны обоснования всех восьми факторов")
        return self


class Research(StrictResult):
    status: Literal["verified", "needs_verification", "closed"]
    card: Card | None = None
    assessment: Assessment | None = None
    warnings: list[str] = Field(default_factory=list, max_length=50)


RESULTS: dict[str, type[StrictResult]] = {
    "intro": Introduction,
    "discover": Discoveries,
    "research": Research,
}


def parse_result(
    operation: str, result: dict[str, Any], map_resolver: MapResolver | None = None
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """An optional map must not invalidate otherwise useful research or a whole search.

    Keep strict validation for the rest of the contract. Diagnostics belong to the
    job report, outside the model's bounded warnings array; the saved response stays raw.
    """
    cleaned = deepcopy(result)
    warnings: list[dict[str, str]] = []
    entries: list[tuple[Any, str]] = []
    if operation == "discover" and isinstance(cleaned.get("restaurants"), list):
        entries = [
            (item, f"restaurants[{index}].mapsUrl")
            for index, item in enumerate(cleaned["restaurants"])
        ]
    elif operation == "research":
        entries = [(cleaned.get("card"), "card.mapsUrl")]
    urls = [
        item["mapsUrl"]
        for item, _ in entries
        if isinstance(item, dict) and isinstance(item.get("mapsUrl"), str)
    ]
    # Called once, before validation/application and outside any database transaction.
    resolved = map_resolver(urls) if map_resolver and urls else {}

    def optional_map(item: Any, path: str) -> None:
        if not isinstance(item, dict) or item.get("mapsUrl") in (None, ""):
            return
        value = item["mapsUrl"]
        if isinstance(value, str) and value in resolved:
            replacement = RestaurantInput.maps(resolved[value])
            if replacement != value:
                item["mapsUrl"] = replacement
                warnings.append(
                    {
                        "path": path,
                        "reason": "Карточка с тем же ID проверена; "
                        "ссылка заменена на " + replacement,
                    }
                )
                return
        try:
            if not isinstance(value, str):
                raise ValueError("Карта должна быть HTTPS-ссылкой на Яндекс Карты")
            RestaurantInput.maps(value)
        except ValueError:
            item["mapsUrl"] = None
            # Do not copy a rejected URL (possibly with credentials) into public diagnostics.
            warnings.append(
                {
                    "path": path,
                    "reason": "Неподдержанная или некорректная ссылка карты исключена; "
                    "остальные данные сохранены. Известная карта не заменяется.",
                }
            )

    for item, path in entries:
        optional_map(item, path)
    return RESULTS[operation].model_validate(cleaned).model_dump(mode="json"), warnings
