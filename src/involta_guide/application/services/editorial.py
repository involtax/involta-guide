import copy
from uuid import uuid4

from involta_guide.application.ports.repositories import Record, Repository
from involta_guide.domain.entities import Conflict, NotFound, RuleViolation
from involta_guide.domain.rules import assert_revision, restaurant_rank_key, restaurant_score


def require(repo: Repository, table: str, key: str | int, *, lock: bool = False) -> Record:
    row = repo.get(table, key, lock=lock)
    if row is None:
        raise NotFound("Запись не найдена")
    return row


# A page has one mutable state. The token only fences concurrent writes; it has no history.
CARD_FIELDS = {
    "name": "name",
    "cuisine": "cuisine",
    "description": "description",
    "mapsUrl": "maps_url",
    "phone": "phone",
    "sources": "sources",
    "openingHours": "opening_hours",
    "alexStar": "alex_star",
    "latitude": "latitude",
    "longitude": "longitude",
}


def current_content(repo: Repository, location: Record) -> Record:
    cards = []
    report = []
    for record in sorted(
        repo.rows("restaurants", {"location_id": location["id"], "active": 1}, limit=10000),
        key=restaurant_rank_key,
    ):
        if isinstance(record["ranking"], dict):
            report.append(
                {
                    **record["ranking"],
                    "name": record["name"],
                    "mapsUrl": record["maps_url"],
                    "totalScore": restaurant_score(record),
                }
            )
        cards.append(
            {
                "id": record["id"],
                "locationId": location["id"],
                "locationName": location["name"],
                **{field: record[column] for field, column in CARD_FIELDS.items()},
            }
        )
    return {"intro": location["intro"], "restaurants": cards, "rankingReport": report}


def field_diff(before: Record, after: Record) -> list[Record]:
    """Changed fields only, including additions/removals and explicit collection order."""
    changes: list[Record] = []

    def walk(old: Record, new: Record, path: list[str]) -> None:
        for key in sorted(old.keys() | new.keys()):
            left, right = old.get(key), new.get(key)
            if key in old and key in new and left == right:
                continue
            if (
                (isinstance(left, dict) or isinstance(right, dict))
                and (left is None or isinstance(left, dict))
                and (right is None or isinstance(right, dict))
            ):
                walk(left or {}, right or {}, [*path, key])
            else:
                changes.append({"path": [*path, key], "before": left, "after": right})

    left, right = copy.deepcopy(before), copy.deepcopy(after)
    old_cards, new_cards = left.pop("restaurants", []), right.pop("restaurants", [])
    walk(left, right, [])
    walk({r["id"]: r for r in old_cards}, {r["id"]: r for r in new_cards}, ["restaurants"])
    old_order, new_order = [r["id"] for r in old_cards], [r["id"] for r in new_cards]
    if old_order != new_order:
        changes.append({"path": ["restaurantOrder"], "before": old_order, "after": new_order})
    return changes


def save(
    repo: Repository,
    location_id: int,
    content: Record,
    expected: str | None,
    *,
    job_id: str | None = None,
    review_id: str | None = None,
) -> str | None:
    """One owner, one row lock, one atomic source diff. No second page composition store."""
    from datetime import UTC, datetime

    if bool(job_id) == bool(review_id):
        raise RuleViolation("Изменение должно принадлежать запуску наполнения или отзыву")
    location = require(repo, "locations", location_id, lock=True)
    if location["is_hub"] or location["reset_pending"]:
        raise Conflict("Локация не принимает изменения подборки")
    assert_revision(location["revision"], expected)
    table, identity = ("jobs", job_id) if job_id else ("reviews", review_id)
    assert identity is not None
    source = require(repo, table, identity, lock=True)
    if source["location_id"] != location_id or source["content_epoch"] != location["content_epoch"]:
        raise Conflict("Цикл локации изменился")
    if job_id and (source["kind"] != "generate" or location["initialized_at"] is not None):
        raise Conflict("Подборка уже наполнена; доступен только полный бета-сброс")
    before = current_content(repo, location)
    payload = copy.deepcopy(content)
    ranking = payload.get("rankingReport", [])
    seen = set()
    for card in payload["restaurants"]:
        card["id"] = card.get("id") or str(uuid4())
        if card["id"] in seen:
            raise RuleViolation("Ресторан повторяется в подборке")
        seen.add(card["id"])
        previous = repo.get("restaurants", card["id"], lock=True)
        if previous and previous["location_id"] != location_id:
            raise RuleViolation("Ресторан принадлежит другой локации")
        values = {column: card.get(field) for field, column in CARD_FIELDS.items()}
        values.update(
            phone=card.get("phone") or "",
            sources=card.get("sources") or [],
            alex_star=bool(previous and previous["alex_star"]),
            active=1,
        )
        if job_id:
            candidate = next(
                (
                    r
                    for r in ranking
                    if r["name"] == card["name"]
                    and (not r.get("mapsUrl") or r["mapsUrl"] == card.get("mapsUrl"))
                ),
                None,
            )
            values["ranking"] = (
                {key: value for key, value in candidate.items() if key != "totalScore"}
                if candidate is not None
                else None
            )
        if previous:
            repo.change("restaurants", card["id"], values)
        else:
            repo.add("restaurants", {"id": card["id"], "location_id": location_id, **values})
    for previous in repo.rows("restaurants", {"location_id": location_id}, limit=10000):
        if previous["id"] not in seen and previous["active"]:
            repo.change("restaurants", previous["id"], {"active": 0})
    if job_id:
        # The catalogue owns every discovered name; only selected cards are public.
        known = {
            " ".join(r["name"].casefold().split())
            for r in repo.rows("restaurants", {"location_id": location_id}, limit=10000)
        }
        candidates = payload.get("candidateReview") or []
        rejected = {
            " ".join(r["name"].casefold().split())
            for r in candidates
            if r.get("exclusionCode") in {"outside_area", "not_restaurant", "duplicate"}
            or "location" in r.get("verificationIssues", [])
        }
        discoveries = []
        for item in [*candidates, *payload.get("discoveredRestaurants", [])]:
            name = " ".join(item["name"].split())
            key = name.casefold()
            if not key or key in known or key in rejected:
                continue
            record = repo.add(
                "restaurants",
                {
                    "id": str(uuid4()),
                    "location_id": location_id,
                    "name": name,
                    "active": 0,
                    "sources": item.get("sources") or [],
                },
            )
            known.add(key)
            discoveries.append({"id": record["id"], "name": name, "sources": record["sources"]})
        payload["catalogueAdditions"] = discoveries
    repo.change("locations", location_id, {"intro": payload["intro"]})
    diff = field_diff(before, current_content(repo, {**location, "intro": payload["intro"]}))
    now = datetime.now(UTC)
    if not diff and not job_id:
        return location["revision"]
    revision = str(uuid4())
    values = {"revision": revision, "updated_at": now}
    if job_id:
        values["initialized_at"] = now
    repo.change("locations", location_id, values)
    events = copy.deepcopy(source["changes"])
    if diff:
        events.append({"location_id": location_id, "at": now.isoformat(), "diff": diff})
    changes = {"changes": events}
    if job_id:
        changes["report"] = {k: v for k, v in payload.items() if k not in {"intro", "restaurants"}}
    repo.change(table, identity, changes)
    return revision
