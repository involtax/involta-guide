import copy
import hashlib
import json
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine
from sqlalchemy import text as sql_text

from involta_guide.application.commands.schemas import RestaurantInput
from involta_guide.application.commands.voice import Decision as Decision
from involta_guide.application.services.editorial import current_content, require, save
from involta_guide.domain.entities import Conflict, RuleViolation
from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow
from involta_voice.db import encoded, execute, row


class VoiceEditorial:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def decide(self, review_id: str, command: Decision, session_id: str) -> dict[str, Any]:
        request_hash = hashlib.sha256(
            json.dumps(
                {"review": review_id, "command": command.model_dump(mode="json")}, sort_keys=True
            ).encode()
        ).hexdigest()
        with PostgresUow(self.engine) as uow:
            repo, conn = uow.repo, uow.session.connection()
            execute(
                conn,
                "SELECT pg_advisory_xact_lock(hashtext(:key))",
                key=str(command.idempotency_key),
            )
            previous = row(
                conn,
                "SELECT * FROM review_decisions WHERE idempotency_key=:key",
                key=str(command.idempotency_key),
            )
            if previous:
                if previous["request_hash"] != request_hash:
                    raise Conflict("Ключ решения уже использован для другого изменения")
                return dict(previous["result"])
            from datetime import UTC, datetime

            session = require(repo, "admin_sessions", session_id, lock=True)
            if session["revoked"] or session["expires_at"] <= datetime.now(UTC):
                raise RuleViolation("Сессия редактора завершена")
            review = require(repo, "reviews", review_id)
            if review["alex_session_id"] or review.get("admin_session_id"):
                raise RuleViolation("Редакторская голосовая команда обрабатывается отдельно")
            execute(conn, "SELECT id FROM locations ORDER BY id FOR UPDATE")
            location = require(repo, "locations", review["location_id"], lock=True)
            review = require(repo, "reviews", review_id, lock=True)
            text = require(repo, "review_texts", review_id, lock=True)
            link = repo.get("voice_links", review_id, lock=True)
            active = link["active_run"] if link else 0
            if command.review_revision != text["revision"] or command.processing_run != active:
                raise Conflict("Отзыв или выбранная обработка изменились. Обновите сравнение.")
            page_revision = location["published_version"]
            before = {
                "text": copy.deepcopy(text),
                "moderation": review["moderation"],
                "fraud": review["fraud"],
                "result": review["result"],
                "run": active,
                "page_revision": page_revision,
                "published_revision": location["published_version"],
            }
            result: dict[str, Any] = {}
            if command.action == "edit-text":
                if command.text is None or not command.text.strip():
                    raise RuleViolation("Текст отзыва не может быть пустым")
                repo.change("review_texts", review_id, {"editorial_text": command.text})
            elif command.action in ("bind", "bind-fragment"):
                if command.restaurant_id is not None:
                    restaurant = require(repo, "restaurants", str(command.restaurant_id))
                    if restaurant["location_id"] != location["id"] and not repo.allowed(
                        restaurant["id"], location["id"]
                    ):
                        raise RuleViolation("Ресторан не относится к локации")
                binding = str(command.restaurant_id) if command.restaurant_id else None
                if command.action == "bind-fragment":
                    if command.fragment_number is None or not link:
                        raise RuleViolation("Выберите фрагмент")
                    fragment = row(
                        conn,
                        "SELECT fragment FROM voice_fragments WHERE review_id=:id AND run=:run AND number=:n",
                        id=review_id,
                        run=active,
                        n=command.fragment_number,
                    )
                    if fragment is None:
                        raise RuleViolation("Фрагмент не найден")
                    before["fragment"] = fragment
                    before["binding"] = row(
                        conn,
                        "SELECT * FROM review_fragment_bindings WHERE review_id=:id AND run=:run AND number=:n",
                        id=review_id,
                        run=active,
                        n=command.fragment_number,
                    )
                    execute(
                        conn,
                        """INSERT INTO review_fragment_bindings(review_id,run,number,restaurant_id)
                        VALUES(:id,:run,:n,:restaurant) ON CONFLICT(review_id,run,number) DO UPDATE
                        SET restaurant_id=excluded.restaurant_id,revision=review_fragment_bindings.revision+1""",
                        id=review_id,
                        run=active,
                        n=command.fragment_number,
                        restaurant=binding,
                    )
                    result.update(fragment=command.fragment_number, restaurant_id=binding)
                else:
                    repo.change("review_texts", review_id, {"restaurant_id": binding})
            elif command.action in ("approve", "reject", "fraud", "reset"):
                decision = {
                    "approve": "approved",
                    "reject": "rejected",
                    "fraud": "rejected",
                    "reset": "pending",
                }[command.action]
                values: dict[str, Any] = {
                    "moderation": decision,
                    "fraud": int(command.action == "fraud"),
                    "reason": command.reason,
                }
                if command.action == "approve" and link:
                    selected = row(
                        conn,
                        "SELECT analysis FROM voice_results WHERE review_id=:id AND run=:run",
                        id=review_id,
                        run=active,
                    )
                    if selected:
                        values["result"] = copy.deepcopy(selected["analysis"])
                        for binding_row in conn.execute(
                            sql_text(
                                "SELECT number,restaurant_id FROM review_fragment_bindings WHERE review_id=:id AND run=:run"
                            ),
                            {"id": review_id, "run": active},
                        ).mappings():
                            values["result"]["entries"][binding_row["number"]]["restaurantId"] = (
                                str(binding_row["restaurant_id"])
                                if binding_row["restaurant_id"]
                                else None
                            )
                if command.action == "approve":
                    if review["moderation"] != "pending":
                        raise Conflict("Пакет уже рассмотрен")
                    result.update(
                        self.apply_package(repo, conn, review_id, location, active, command, before)
                    )
                    repo.change("review_texts", review_id, {"public": True})
                repo.change("reviews", review_id, values)
                if command.action != "approve":
                    repo.change("review_texts", review_id, {"public": False})
            elif command.action == "activate-run":
                if (
                    not link
                    or not command.run
                    or not row(
                        conn,
                        "SELECT 1 FROM voice_results WHERE review_id=:id AND run=:run",
                        id=review_id,
                        run=command.run,
                    )
                ):
                    raise RuleViolation("Нет результата выбранной обработки")
                repo.change("voice_links", review_id, {"active_run": command.run})
                result["active_run"] = command.run
            elif command.action in ("edit-proposal", "exclude-proposal"):
                if review["moderation"] != "pending":
                    raise Conflict("Решение уже принято; результат не редактируется")
                proposal = require(repo, "proposals", str(command.proposal_id), lock=True)
                info = repo.get("voice_proposals", str(command.proposal_id), lock=True)
                if proposal["review_id"] != review_id or (
                    info
                    and (info["run"] != active or info["revision"] != command.proposal_revision)
                ):
                    raise Conflict("Пакет изменился")
                before["proposal"] = copy.deepcopy(proposal)
                payload = copy.deepcopy(proposal["payload"])
                if command.action == "edit-proposal":
                    payload["changes"] = command.changes
                    if command.restaurant_id:
                        payload["restaurantId"] = str(command.restaurant_id)
                repo.change(
                    "proposals",
                    proposal["id"],
                    {
                        "payload": payload,
                        "status": "rejected" if command.action == "exclude-proposal" else "pending",
                    },
                )
                if info:
                    repo.change(
                        "voice_proposals", proposal["id"], {"revision": info["revision"] + 1}
                    )
                result["proposal_after"] = repo.get("proposals", proposal["id"])
            else:
                raise RuleViolation("Неизвестное действие")
            repo.change("review_texts", review_id, {"revision": text["revision"] + 1})
            result["review_revision"] = text["revision"] + 1
            after = {
                "text": repo.get("review_texts", review_id),
                "review": {
                    k: v
                    for k, v in require(repo, "reviews", review_id).items()
                    if k
                    in {
                        "id",
                        "location_id",
                        "transcript",
                        "status",
                        "moderation",
                        "fraud",
                        "reason",
                        "result",
                    }
                },
                "result": result,
            }
            execute(
                conn,
                """INSERT INTO review_decisions(id,review_id,actor,session_id,action,idempotency_key,
                request_hash,run,before_data,after_data,expected,result,reason)
                VALUES(:id,:review,'guide-editor',:session,:action,:key,:hash,:run,CAST(:before AS jsonb),
                CAST(:after AS jsonb),CAST(:expected AS jsonb),CAST(:result AS jsonb),:reason)""",
                id=str(uuid4()),
                review=review_id,
                session=session_id,
                action=command.action,
                key=str(command.idempotency_key),
                hash=request_hash,
                run=active,
                before=encoded(before),
                after=encoded(after),
                expected=encoded(
                    {
                        "review_revision": command.review_revision,
                        "proposal_revision": command.proposal_revision,
                        "page_revision": command.page_revision,
                        "published_revision": command.published_revision,
                        "run": command.processing_run,
                    }
                ),
                result=encoded(result),
                reason=command.reason,
            )
            return json.loads(encoded(result))

    def apply_package(
        self,
        repo: Any,
        conn: Any,
        review_id: str,
        location: dict[str, Any],
        active: int,
        command: Decision,
        before: dict[str, Any],
    ) -> dict[str, Any]:
        proposals = [
            dict(p)
            for p in conn.execute(
                sql_text(
                    "SELECT p.* FROM proposals p LEFT JOIN voice_proposals v ON v.proposal_id=p.id WHERE p.review_id=:id AND p.status='pending' AND coalesce(v.run,0)=:run ORDER BY p.id FOR UPDATE OF p"
                ),
                {"id": review_id, "run": active},
            ).mappings()
        ]
        expected = str(command.page_revision) if command.page_revision else None
        if proposals and expected != location["published_version"]:
            raise Conflict("Страница изменилась. Обновите сравнение.")
        content = current_content(repo, location)
        before["content"] = copy.deepcopy(content)
        before["package"] = copy.deepcopy(proposals)
        changed_ids = set()
        for proposal in proposals:
            payload = proposal["payload"]
            changes = payload.get("changes", {})
            if set(changes) - {
                "name",
                "cuisine",
                "description",
                "mapsUrl",
                "phone",
                "openingHours",
            }:
                raise RuleViolation("Недопустимое поле пакета")
            target = payload.get("restaurantId")
            current = next((r for r in content["restaurants"] if r.get("id") == target), None)
            if proposal["kind"] == "edit" and current is None:
                raise RuleViolation("Ресторан правки не найден")
            original = {
                k: v for k, v in (current or {}).items() if k in RestaurantInput.model_fields
            }
            final = {**original, **changes}
            if final.get("phone") is None:
                final["phone"] = ""
            validated = RestaurantInput.model_validate(final).model_dump(mode="json")
            if current is None:
                if not validated["phone"]:
                    raise RuleViolation("Для нового ресторана нужен телефон")
                content["restaurants"].append(validated)
            else:
                changed_ids.add(current["id"])
                current.update(validated)
        # Shared cards affect all linked cities. Lock and snapshot every affected page
        # before writing global restaurant fields; history is never overlaid at read time.
        affected = [
            dict(r)
            for r in conn.execute(
                sql_text(
                    "SELECT DISTINCT l.* FROM locations l JOIN restaurant_locations x ON x.location_id=l.id WHERE x.restaurant_id = ANY(CAST(:ids AS uuid[])) AND l.id<>:id ORDER BY l.id"
                ),
                {"ids": list(changed_ids), "id": location["id"]},
            ).mappings()
        ]
        if affected and {
            str(item["id"]): str(item["published_version"]) if item["published_version"] else None
            for item in affected
        } != {
            str(item["id"]): command.affected_revisions.get(str(item["id"])) for item in affected
        }:
            raise Conflict("Связанные страницы изменились. Обновите сравнение.")
        before["affected"] = {str(item["id"]): current_content(repo, item) for item in affected}
        versions = {}
        if content != before["content"]:
            version = save(repo, location["id"], content, expected, source="review")
            repo.link("version_reviews", {"version_id": version, "review_id": review_id})
            versions[location["id"]] = version
            for other in affected:
                version = save(
                    repo,
                    str(other["id"]),
                    current_content(repo, other),
                    str(other["published_version"]) if other["published_version"] else None,
                    source="review",
                )
                repo.link("version_reviews", {"version_id": version, "review_id": review_id})
                versions[str(other["id"])] = version
        for proposal in proposals:
            repo.change("proposals", proposal["id"], {"status": "approved"})
        return {"versions": versions, "applied_package": proposals, "content_after": content}
