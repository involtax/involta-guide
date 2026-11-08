from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Mention(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    restaurant_id: UUID | None
    sentiment: Literal[-1, 1] | None
    suggested: bool
    evidence: str = Field(min_length=1, max_length=2000)
    # Older saved responses have no isolated source spelling.
    source_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Дословное написание названия в исходном отзыве и evidence; null, если не выделено",
    )


class Analysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["ok", "humor", "bugs", "other", "watched"]
    author_name: str | None = Field(max_length=100)
    edited_text: str = Field(max_length=20000)
    mentions: list[Mention] = Field(max_length=30)
    reason: str = Field(max_length=2000)

    @classmethod
    def response_schema(cls) -> dict:
        schema = cls.model_json_schema()
        # OpenAI strict schemas require nullable properties to be required, too.
        # Parsing remains compatible with already accepted/saved responses.
        mention = schema["$defs"]["Mention"]
        mention["required"] = list(mention["properties"])
        mention["properties"]["source_name"].pop("default", None)
        return schema
