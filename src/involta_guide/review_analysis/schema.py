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


class Analysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["ok", "humor", "bugs", "other"]
    author_name: str | None = Field(max_length=100)
    edited_text: str = Field(max_length=20000)
    mentions: list[Mention] = Field(max_length=30)
    reason: str = Field(max_length=2000)
