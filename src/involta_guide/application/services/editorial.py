import copy
from uuid import uuid4

from involta_guide.application.ports.repositories import Record, Repository
from involta_guide.domain.entities import Conflict, NotFound, RuleViolation
from involta_guide.domain.rules import assert_revision


def require(repo: Repository, table: str, key: str, *, lock: bool = False) -> Record:
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
        key=lambda r: (r["position"], r["number"]),
    ):
        if record["ranking"]:
            report.append(record["ranking"])
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
    location_id: str,
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
    for position, card in enumerate(payload["restaurants"], 1):
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
            position=position,
            active=1,
        )
        if job_id:
            values["ranking"] = next(
                (
                    r
                    for r in ranking
                    if r["name"] == card["name"]
                    and (not r.get("mapsUrl") or r["mapsUrl"] == card.get("mapsUrl"))
                ),
                None,
            )
        if previous:
            repo.change("restaurants", card["id"], values)
        else:
            repo.add("restaurants", {"id": card["id"], "location_id": location_id, **values})
    for previous in repo.rows("restaurants", {"location_id": location_id}, limit=10000):
        if previous["id"] not in seen and previous["active"]:
            repo.change("restaurants", previous["id"], {"active": 0})
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
