"""Read current public names in one transaction; no separately maintained catalogue."""

from typing import Any

from sqlalchemy import Engine, text

from involta_guide.domain.rules import KINDS
from involta_guide.infrastructure.persistence.repositories.public_catalogue import PUBLIC


def catalogue(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = (
            connection.execute(
                text(f"""WITH visible AS (
          SELECT l.id,l.name,l.slug,l.aliases,l.kind,l.region,l.initialized_at,l.priority,
                 l.is_hub,l.reset_pending,p.name parent_name
          FROM locations l LEFT JOIN locations p ON p.id=l.parent_id
          WHERE l.active=1 AND {PUBLIC}
        )
        SELECT 'location' kind,l.name,l.slug location,l.aliases,l.name location_name,
               l.aliases location_aliases,l.region,l.parent_name,l.kind location_kind,
               NULL::bigint number,l.slug entity_key,l.initialized_at,l.priority
        FROM visible l
        UNION ALL
        SELECT 'restaurant',r.name,l.slug,ARRAY[]::text[],l.name,l.aliases,l.region,
               l.parent_name,l.kind,r.number,r.id::text,l.initialized_at,l.priority
        FROM restaurants r JOIN visible l ON l.id=r.location_id
        WHERE r.active=1 AND l.initialized_at IS NOT NULL AND NOT l.is_hub AND NOT l.reset_pending
        ORDER BY kind DESC,initialized_at DESC NULLS LAST,name,entity_key""")
            )
            .mappings()
            .all()
        )
    result = []
    for row in rows:
        item = dict(row)
        item["id"] = item["kind"] + ":" + item.pop("entity_key")
        item["anchor"] = "restaurant-" + item["id"].split(":", 1)[1] if item["number"] else ""
        item["context"] = (
            item["location_name"]
            if item["kind"] == "restaurant"
            else " · ".join(
                dict.fromkeys(
                    value
                    for value in (
                        KINDS[item["location_kind"]] if item["location_kind"] != "city" else None,
                        item["parent_name"],
                        item["region"],
                    )
                    if value
                )
            )
        )
        result.append(item)
    # Same baseline as the compact location snapshot; relevance always takes precedence.
    result.sort(
        key=lambda item: (
            item["kind"] == "restaurant",
            item["initialized_at"] is None,
            {"high": 0, "medium": 1, "low": 2}[item["priority"]],
            item["name"],
            item["id"],
        )
    )
    return result
