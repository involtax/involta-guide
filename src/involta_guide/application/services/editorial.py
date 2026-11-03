import copy
import json
from uuid import uuid4

from involta_guide.application.ports.repositories import Record, Repository, UowFactory
from involta_guide.domain.entities import Conflict, NotFound, RuleViolation
from involta_guide.domain.rules import assert_proposal, assert_revision


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
    content = copy.deepcopy(location["page_data"])
    content["intro"] = location["intro"]
    content.setdefault("restaurants", [])
    for card in content["restaurants"]:
        record = require(repo, "restaurants", card["id"])
        card.update({target: record[source] for target, source in CARD_FIELDS.items()})
        card["locationId"] = record["location_id"]
        card["locationName"] = require(repo, "locations", record["location_id"])["name"]
    return content


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
    alex: bool = False,
) -> str:
    """Apply current data and append its diff to exactly one source, in the caller's transaction."""
    from datetime import UTC, datetime

    if bool(job_id) == bool(review_id):
        raise RuleViolation("Изменение должно принадлежать запуску наполнения или отзыву")
    locations = repo.lock_locations()
    location = require(repo, "locations", location_id, lock=True)
    assert_revision(location["revision"], expected)
    before = current_content(repo, location)
    payload = copy.deepcopy(content)
    payload.pop("warnings", None)
    source_table, source_id = ("jobs", job_id) if job_id else ("reviews", review_id)
    assert source_id is not None
    source = require(repo, source_table, source_id, lock=True)
    if source["location_id"] != location_id or (job_id and source["kind"] != "generate"):
        raise RuleViolation("Источник изменения не соответствует локации или операции")
    # Lock all locations in stable order: canonical restaurant edits can affect several pages.
    affected: dict[str, Record] = {}
    card_ids = {card.get("id") for card in payload["restaurants"]}
    for other in locations:
        if other["id"] != location_id and any(
            card["id"] in card_ids for card in other["page_data"].get("restaurants", [])
        ):
            affected[other["id"]] = current_content(repo, other)
    for card in payload["restaurants"]:
        card["id"] = card.get("id") or str(uuid4())
        previous = repo.get("restaurants", card["id"], lock=True)
        if (
            previous
            and previous["location_id"] != location_id
            and not repo.allowed(card["id"], location_id)
        ):
            raise RuleViolation("Сначала свяжите ресторан с этой локацией")
        card["alexStar"] = (
            bool(card.get("alexStar")) if alex else bool(previous and previous["alex_star"])
        )
        values = {column: card.get(field) for field, column in CARD_FIELDS.items()}
        values.update(phone=card.get("phone") or "", sources=card.get("sources") or [])
        if previous:
            repo.change("restaurants", card["id"], values)
        else:
            repo.add("restaurants", {"id": card["id"], "location_id": location_id, **values})
        repo.link("restaurant_locations", {"restaurant_id": card["id"], "location_id": location_id})
    data = {key: value for key, value in payload.items() if key != "intro"}
    data["restaurants"] = [
        {
            key: value
            for key, value in card.items()
            if key not in CARD_FIELDS and key not in {"locationId", "locationName"}
        }
        for card in payload["restaurants"]
    ]
    revision = str(uuid4())
    now = datetime.now(UTC)
    repo.change(
        "locations",
        location_id,
        {
            "intro": payload["intro"],
            "page_data": data,
            "revision": revision,
            "updated_at": now,
        },
    )
    events = copy.deepcopy(source["changes"])
    for key, previous_content in {location_id: before, **affected}.items():
        updated = require(repo, "locations", key)
        diff = field_diff(previous_content, current_content(repo, updated))
        if not diff:
            continue
        if key != location_id:
            repo.change("locations", key, {"revision": str(uuid4()), "updated_at": now})
        events.append({"location_id": key, "at": now.isoformat(), "diff": diff})
    repo.change(source_table, source_id, {"changes": events})
    return revision


class EditorialService:
    def __init__(self, uow: UowFactory) -> None:
        self.uow = uow

    def moderate(self, review_id: str, decision: str, reason: str) -> None:
        if decision not in ("approved", "rejected", "fraud", "reset"):
            raise RuleViolation("Неизвестное решение")
        with self.uow() as uow:
            if uow.repo.get("voice_links", review_id):
                raise Conflict("Используйте карточку голосового отзыва с проверкой версии")
            require(uow.repo, "reviews", review_id, lock=True)
            uow.repo.change(
                "reviews",
                review_id,
                {
                    "moderation": "rejected"
                    if decision == "fraud"
                    else "pending"
                    if decision == "reset"
                    else decision,
                    "fraud": int(decision == "fraud"),
                    "reason": reason[:2000],
                },
            )
            uow.repo.audit("review_" + decision, review_id, reason[:2000])

    def proposal(self, proposal_id: str, edited: Record | None) -> str | None:
        with self.uow() as uow:
            repo = uow.repo
            repo.lock_locations()
            p = require(repo, "proposals", proposal_id, lock=True)
            if repo.get("voice_proposals", proposal_id):
                raise Conflict("Используйте карточку голосового отзыва с проверкой версии")
            review = require(repo, "reviews", p["review_id"], lock=True)
            if edited is None:
                if p["status"] != "pending":
                    raise Conflict("Предложение уже обработано")
                repo.change("proposals", proposal_id, {"status": "rejected"})
                repo.audit("proposal_reject", proposal_id)
                return None
            assert_proposal(p["status"], review["moderation"], bool(review["fraud"]))
            location = require(repo, "locations", p["location_id"], lock=True)
            content = current_content(repo, location)
            if p["kind"] == "restaurant":
                if any(
                    r["name"].casefold() == edited["name"].casefold()
                    for r in content["restaurants"]
                ):
                    raise RuleViolation("Возможный дубликат ресторана")
                content["restaurants"].append(edited)
            else:
                current = next(
                    (
                        r
                        for r in content["restaurants"]
                        if r.get("id") == p["payload"].get("restaurantId")
                    ),
                    None,
                )
                if current is None:
                    raise RuleViolation("Ресторан отсутствует на странице")
                current.update(
                    {
                        k: v
                        for k, v in edited.items()
                        if k not in ("id", "locationId", "locationName", "alexStar")
                    }
                )
            revision = save(
                repo, location["id"], content, location["revision"], review_id=review["id"]
            )
            repo.change("proposals", proposal_id, {"status": "approved"})
            repo.audit("proposal_applied", location["id"], review["id"])
            return revision

    def apply_alex(
        self, repo: Repository, review: Record, commands: Record, expected: str | None
    ) -> None:
        from datetime import UTC, datetime

        repo.lock_locations()
        admin_session = review.get("admin_session_id")
        session = require(
            repo,
            "admin_sessions" if admin_session else "alex_sessions",
            admin_session or review["alex_session_id"],
            lock=True,
        )
        if session["revoked"] or session["expires_at"] <= datetime.now(UTC):
            raise RuleViolation("Сессия Алекса завершена")
        location = require(repo, "locations", review["location_id"], lock=True)
        assert_revision(location["revision"], expected)
        content = current_content(repo, location)
        before = copy.deepcopy(content)
        if commands.get("intro") is not None:
            content["intro"] = commands["intro"]
        for action in commands["actions"]:
            changes = action["changes"]
            key = action["restaurantId"]
            if action["kind"] == "add":
                if key or any(
                    r["name"].casefold() == changes.get("name", "").casefold()
                    for r in content["restaurants"]
                ):
                    raise RuleViolation("Дубликат или неверная новая запись")
                content["restaurants"].append(changes)
            else:
                current = next((r for r in content["restaurants"] if r.get("id") == key), None)
                if current is None:
                    raise RuleViolation("Не удалось однозначно определить ресторан")
                if action["kind"] == "remove":
                    content["restaurants"].remove(current)
                else:
                    if current.get("locationId") != location["id"] and set(changes) - {"alexStar"}:
                        raise RuleViolation("Откройте основную локацию ресторана")
                    current.update(changes)
        # Adapter validates external command structure; full merged content must also pass boundary validation.
        from involta_guide.application.commands.schemas import ContentInput

        changed = content != before
        content = ContentInput.model_validate(content).model_dump(mode="json", exclude_none=True)
        revision = None
        if changed:
            revision = save(
                repo, location["id"], content, expected, review_id=review["id"], alex=True
            )
        elif not commands.get("comment"):
            raise RuleViolation("Нет однозначной команды")
        repo.change(
            "reviews",
            review["id"],
            {"result": commands, "status": "processed", "moderation": "approved", "error": None},
        )
        change = repo.rows("alex_changes", {"review_id": review["id"]})
        if change:
            repo.change(
                "alex_changes",
                change[0]["id"],
                {
                    "status": "applied",
                    "detail": "Применено" if revision else "Комментарий сохранён",
                },
            )
        repo.audit(
            "alex_publish" if revision else "alex_comment",
            location["id"],
            json.dumps({"reviewId": review["id"], "revision": revision}),
        )
