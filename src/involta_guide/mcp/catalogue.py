"""Project public records explicitly. Never return internal rows or trigger filling."""

from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

from involta_guide.application.services.editorial import current_content
from involta_guide.config import Settings
from involta_guide.infrastructure.persistence.repositories.public_catalogue import PUBLIC
from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow
from involta_guide.review_analysis.visibility import PUBLIC_REVIEW

from .models import (
    LocationCard,
    LocationReference,
    LocationResult,
    RestaurantCard,
    RestaurantResult,
    Review,
    Reviews,
)


class PublicGuide:
    def __init__(self, engine: Engine, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings

    def reference(self, row: dict[str, Any]) -> LocationReference:
        return LocationReference(
            id=row["id"],
            location=row["slug"],
            name=row["name"],
            kind=row["kind"],
            region=row["region"],
            url=self.settings.url("/" + row["slug"]),
        )

    def restaurant_card(self, card: dict[str, Any], location: LocationReference) -> RestaurantCard:
        return RestaurantCard(
            id=card["id"],
            name=card["name"],
            location=location,
            url=location.url + "#restaurant-" + card["id"],
            cuisine=card.get("cuisine") or None,
            description=card.get("description") or None,
            phone=card.get("phone") or None,
            maps_url=card.get("mapsUrl") or None,
            latitude=card.get("latitude"),
            longitude=card.get("longitude"),
            opening_hours=card.get("openingHours") or None,
            sources=card.get("sources") or [],
        )

    def location(self, identifier: str) -> LocationResult:
        key = identifier.strip().lower().replace("ё", "е")
        with PostgresUow(self.engine) as work:
            work.session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            repo = work.repo
            matches = repo.query(
                f"""SELECT l.* FROM locations l WHERE l.active=1 AND {PUBLIC}
                AND (l.slug=:slug OR replace(lower(l.name),'ё','е')=:name)
                ORDER BY l.region,l.name,l.id""",
                {"slug": identifier.strip().lower(), "name": key},
            )
            direct = [row for row in matches if row["slug"] == identifier.strip().lower()]
            matches = direct or matches
            if not matches:
                return LocationResult(
                    status="not_found",
                    message="Локация не найдена. Передайте точное название или slug из URL гида.",
                )
            if len(matches) > 1:
                return LocationResult(
                    status="ambiguous",
                    alternatives=[self.reference(row) for row in matches],
                    message="Уточните регион и повторите Location с выбранным location (slug).",
                )
            row = matches[0]
            reference = self.reference(row)
            ready = (
                row["initialized_at"] is not None and not row["is_hub"] and not row["reset_pending"]
            )
            cards = current_content(repo, row)["restaurants"] if ready else []
            public_rows = repo.query(
                f"""SELECT l.* FROM locations l WHERE l.active=1 AND {PUBLIC}
                AND (l.parent_id=:id OR l.id=ANY(CAST(:nearby AS bigint[]))) ORDER BY l.name,l.id""",
                {"id": row["id"], "nearby": row["nearby_location_ids"]},
            )
            visible = {item["id"]: item for item in public_rows}
            nearby = [
                self.reference(visible[key]) for key in row["nearby_location_ids"] if key in visible
            ]
            reviews = repo.query(
                f"""SELECT id::text,coalesce(edited_text,transcript) transcript,created_at,count(*) OVER() total FROM reviews r
                WHERE location_id=:id AND {PUBLIC_REVIEW}
                AND nullif(btrim(transcript),'') IS NOT NULL
                ORDER BY created_at DESC,number DESC LIMIT 10""",
                {"id": row["id"]},
            )
            return LocationResult(
                status="found",
                location=LocationCard(
                    **reference.model_dump(),
                    content_status="hub"
                    if row["is_hub"]
                    else "published"
                    if ready
                    else "not_filled",
                    intro=row["intro"] if ready else None,
                    updated_at=row["updated_at"],
                    restaurants=[self.restaurant_card(card, reference) for card in cards],
                    reviews=Reviews(
                        total=reviews[0]["total"] if reviews else 0,
                        items=[
                            Review(id=r["id"], text=r["transcript"], created_at=r["created_at"])
                            for r in reviews
                        ],
                    ),
                    children=[
                        self.reference(item)
                        for item in public_rows
                        if item["parent_id"] == row["id"]
                    ],
                    nearby=nearby,
                ),
            )

    def restaurant(self, restaurant_id: UUID) -> RestaurantResult:
        with PostgresUow(self.engine) as work:
            work.session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            repo = work.repo
            rows = repo.query(
                f"""SELECT l.* FROM restaurants r JOIN locations l ON l.id=r.location_id
                WHERE r.id=:id AND r.active=1 AND l.active=1 AND {PUBLIC}
                AND l.initialized_at IS NOT NULL AND NOT l.is_hub AND NOT l.reset_pending""",
                {"id": str(restaurant_id)},
            )
            if not rows:
                return RestaurantResult(
                    status="not_found",
                    message="Опубликованный ресторан не найден. Получите restaurant_id из ответа Location.",
                )
            row = rows[0]
            card = next(
                card
                for card in current_content(repo, row)["restaurants"]
                if card["id"] == str(restaurant_id)
            )
            return RestaurantResult(
                status="found", restaurant=self.restaurant_card(card, self.reference(row))
            )
