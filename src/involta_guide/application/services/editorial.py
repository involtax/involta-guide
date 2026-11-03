import copy
import json
from uuid import uuid4

from involta_guide.application.ports.repositories import Record, Repository, UowFactory
from involta_guide.domain.entities import Conflict, NotFound, RuleViolation
from involta_guide.domain.rules import assert_proposal, assert_publishable, assert_revision


def require(repo: Repository, table: str, key: str, *, lock: bool = False) -> Record:
    row = repo.get(table, key, lock=lock)
    if row is None:
        raise NotFound("Запись не найдена")
    return row


def draft(repo: Repository, location: Record) -> Record:
    if location["draft_version"]:
        return copy.deepcopy(require(repo, "versions", location["draft_version"])["content"])
    return {"intro": location["intro"], "restaurants": copy.deepcopy(location["draft"])}


def save(
    repo: Repository,
    location_id: str,
    content: Record,
    expected: str | None,
    *,
    source: str = "manual",
    prompt: str | None = None,
    model: str | None = None,
    alex: bool = False,
    inherit: bool = True,
) -> str:
    location = require(repo, "locations", location_id, lock=True)
    assert_revision(location["draft_version"], expected)
    payload = copy.deepcopy(content)
    payload.pop("warnings", None)
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
            bool(card.get("alexStar"))
            if alex or source == "restore"
            else bool(previous and previous["alex_star"])
        )
        if previous and previous["location_id"] != location_id:
            card["locationId"] = previous["location_id"]
            card["locationName"] = require(repo, "locations", previous["location_id"])["name"]
        else:
            card["locationId"] = location_id
            card["locationName"] = location["name"]
            values = {
                "location_id": location_id,
                "name": card["name"],
                "cuisine": card["cuisine"],
                "description": card["description"],
                "maps_url": card["mapsUrl"],
                "sources": card.get("sources", []),
                "phone": card.get("phone", ""),
                "opening_hours": card.get("openingHours"),
                "alex_star": card["alexStar"] if alex else bool(previous and previous["alex_star"]),
                "latitude": card.get("latitude"),
                "longitude": card.get("longitude"),
            }
            if previous:
                repo.change("restaurants", card["id"], values)
            else:
                repo.add("restaurants", {"id": card["id"], **values})
        if alex and previous:
            repo.change("restaurants", card["id"], {"alex_star": card["alexStar"]})
        repo.link("restaurant_locations", {"restaurant_id": card["id"], "location_id": location_id})
    version = str(uuid4())
    repo.add(
        "versions",
        {
            "id": version,
            "location_id": location_id,
            "number": repo.next_version(location_id),
            "content": payload,
            "source": source,
            "prompt": prompt,
            "model": model,
        },
    )
    repo.change(
        "locations",
        location_id,
        {"intro": payload["intro"], "draft": payload["restaurants"], "draft_version": version},
    )
    if inherit and expected:
        repo.inherit_reviews(expected, version)
    repo.audit("draft", location_id, source)
    return version


def publish(
    repo: Repository, location_id: str, version_id: str, expected_published: str | None
) -> None:
    location = require(repo, "locations", location_id, lock=True)
    if location["published_version"] != expected_published:
        raise Conflict("Опубликованная версия изменилась. Обновите страницу.")
    version = require(repo, "versions", version_id)
    if version["location_id"] != location_id:
        raise RuleViolation("Версия принадлежит другой локации")
    assert_publishable(version["content"])
    repo.change("versions", version_id, {"published": 1})
    repo.change("locations", location_id, {"published_version": version_id})
    repo.audit("publish", location_id, version_id)


class EditorialService:
    def __init__(self, uow: UowFactory) -> None:
        self.uow = uow

    def save(self, location_id: str, content: Record, expected: str | None) -> str:
        with self.uow() as uow:
            return save(uow.repo, location_id, content, expected)

    def publish(self, location_id: str, version_id: str, expected: str | None) -> None:
        with self.uow() as uow:
            publish(uow.repo, location_id, version_id, expected)

    def restore(self, location_id: str, version_id: str, expected: str | None) -> str:
        with self.uow() as uow:
            version = require(uow.repo, "versions", version_id)
            if version["location_id"] != location_id:
                raise RuleViolation("Чужая версия")
            new = save(
                uow.repo, location_id, version["content"], expected, source="restore", inherit=False
            )
            uow.repo.inherit_reviews(version_id, new)
            return new

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
            content = draft(repo, location)
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
                    raise RuleViolation("Ресторан отсутствует в черновике")
                current.update(
                    {
                        k: v
                        for k, v in edited.items()
                        if k not in ("id", "locationId", "locationName", "alexStar")
                    }
                )
            version = save(
                repo, location["id"], content, location["draft_version"], source="review"
            )
            repo.link("version_reviews", {"version_id": version, "review_id": review["id"]})
            repo.change("proposals", proposal_id, {"status": "approved"})
            repo.audit("proposal_draft", location["id"], review["id"])
            return version

    def apply_alex(
        self, repo: Repository, review: Record, commands: Record, expected: str | None
    ) -> None:
        from datetime import UTC, datetime

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
        assert_revision(location["draft_version"], expected)
        if expected and expected != location["published_version"]:
            raise Conflict("Сначала проверьте неопубликованный редакторский черновик")
        content = draft(repo, location)
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
        version = None
        if changed:
            version = save(repo, location["id"], content, expected, source="alex", alex=True)
            repo.link("version_reviews", {"version_id": version, "review_id": review["id"]})
            publish(repo, location["id"], version, location["published_version"])
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
                    "version_id": version,
                    "status": "applied",
                    "detail": "Применено" if version else "Комментарий сохранён",
                    "before_content": before,
                    "after_content": content,
                },
            )
        repo.audit(
            "alex_publish" if version else "alex_comment",
            location["id"],
            json.dumps({"reviewId": review["id"], "versionId": version}),
        )
