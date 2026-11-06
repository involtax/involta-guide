from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class LocationReference(BaseModel):
    id: int
    location: str = Field(description="Canonical location slug; pass it to Location.")
    name: str
    kind: str
    region: str
    url: str


class Review(BaseModel):
    id: str
    text: str
    created_at: datetime


class Reviews(BaseModel):
    items: list[Review] = Field(default_factory=list)
    total: int = Field(
        default=0, description="Count of all published nonempty text reviews in this scope."
    )
    limit: int = 10
    status: Literal["available", "not_linked"] = "available"


class RestaurantCard(BaseModel):
    id: str = Field(description="Stable UUID; pass it as restaurant_id to Restaurant.")
    name: str
    location: LocationReference
    url: str
    cuisine: str | None = None
    description: str | None = None
    address: str | None = Field(
        default=None, description="Postal address when stored; never inferred from a map URL."
    )
    phone: str | None = None
    maps_url: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    opening_hours: dict[str, Any] | None = Field(
        default=None,
        description="Local schedule: IANA timezone, week (7 arrays, Monday first) and dated exceptions. Each interval is [start,end] in HH:MM; empty day means closed, equal times mean 24 hours, end before start means next day. Null means unknown.",
    )
    sources: list[str] = Field(default_factory=list)
    reviews: Reviews = Field(default_factory=lambda: Reviews(status="not_linked"))


class LocationCard(LocationReference):
    content_status: Literal["published", "not_filled", "hub"]
    intro: str | None = None
    updated_at: datetime | None = Field(
        default=None, description="Content edit time, not a restaurant verification date."
    )
    restaurants: list[RestaurantCard] = Field(default_factory=list)
    reviews: Reviews = Field(default_factory=Reviews)
    children: list[LocationReference] = Field(default_factory=list)
    nearby: list[LocationReference] = Field(default_factory=list)


class LocationResult(BaseModel):
    status: Literal["found", "not_found", "ambiguous"]
    location: LocationCard | None = None
    alternatives: list[LocationReference] = Field(default_factory=list)
    message: str | None = None


class RestaurantResult(BaseModel):
    status: Literal["found", "not_found"]
    restaurant: RestaurantCard | None = None
    message: str | None = None
