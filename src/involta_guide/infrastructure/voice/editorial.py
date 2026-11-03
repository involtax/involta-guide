import copy
import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine
from sqlalchemy import text as sql_text

from involta_guide.application.commands.schemas import RestaurantInput
from involta_guide.application.services.editorial import current_content, require, save
from involta_guide.domain.entities import Conflict, RuleViolation
from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow
from involta_voice.db import encoded, execute, row


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: UUID
    review_revision: int = Field(ge=1)
    processing_run: int = Field(ge=0)
    action: str
    reason: str = Field(default="", max_length=2000)
    text: str | None = Field(default=None, max_length=30000)
    restaurant_id: UUID | None = None
    proposal_id: UUID | None = None
    proposal_revision: int | None = None
    page_revision: UUID | None = None
    changes: dict[str, Any] = Field(default_factory=dict)
    run: int | None = None
    fragment_number: int | None = Field(default=None, ge=0)
    version_id: UUID | None = None
    published_revision: UUID | None = None


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
            location = require(repo, "locations", review["location_id"], lock=True)
            review = require(repo, "reviews", review_id, lock=True)
            text = require(repo, "review_texts", review_id, lock=True)
            link = repo.get("voice_links", review_id, lock=True)
            active = link["active_run"] if link else 0
            if command.review_revision != text["revision"] or command.processing_run != active:
                raise Conflict("Отзыв или выбранная обработка изменились. Обновите сравнение.")
            candidate = row(
                conn, "SELECT * FROM voice_page_drafts WHERE location_id=:id", id=location["id"]
            )
            page_revision = (
                str(candidate["version_id"]) if candidate else location["published_version"]
            )
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
                repo.change("reviews", review_id, values)
                if command.action != "approve":
                    repo.change("review_texts", review_id, {"public": False})
            elif command.action == "publish-text":
                if review["moderation"] != "approved" or review["fraud"]:
                    raise RuleViolation("Сначала примите отзыв")
                repo.change("review_texts", review_id, {"public": True})
            elif command.action == "hide-text":
                repo.change("review_texts", review_id, {"public": False})
            elif command.action == "publish-page":
                if review["moderation"] != "approved" or review["fraud"]:
                    raise RuleViolation("Сначала примите отзыв")
                target = str(command.version_id) if command.version_id else None
                expected_page = str(command.page_revision) if command.page_revision else None
                expected_published = (
                    str(command.published_revision) if command.published_revision else None
                )
                if not candidate or expected_page != page_revision or target != expected_page:
                    raise Conflict("Черновик изменился. Обновите сравнение.")
                if not any(
                    r["version_id"] == target
                    for r in repo.rows("version_reviews", {"review_id": review_id})
                ):
                    raise RuleViolation("Версия не связана с этим отзывом")
                before["published_content"] = (
                    require(repo, "versions", location["published_version"])["content"]
                    if location["published_version"]
                    else None
                )
                assert target is not None
                base = str(candidate["base_version"]) if candidate["base_version"] else None
                if (
                    expected_published != location["published_version"]
                    or base != expected_published
                ):
                    raise Conflict(
                        "Страница изменилась после подготовки правок. Сбросьте черновик и проверьте предложения заново."
                    )
                if row(
                    conn,
                    "SELECT 1 FROM version_reviews v JOIN reviews r ON r.id=v.review_id WHERE v.version_id=:version AND (r.moderation<>'approved' OR r.fraud<>0)",
                    version=target,
                ):
                    raise RuleViolation(
                        "Один из отзывов черновика отклонён или помечен фродом; сбросьте правки"
                    )
                content = require(repo, "versions", target)["content"]
                published = save(repo, location["id"], content, expected_published, source="review")
                repo.inherit_reviews(target, published)
                execute(
                    conn, "DELETE FROM voice_page_drafts WHERE location_id=:id", id=location["id"]
                )
                result.update(published_version=published, published_content=content)
            elif command.action == "discard-page":
                expected = str(command.page_revision) if command.page_revision else None
                if not candidate or expected != page_revision:
                    raise Conflict("Черновик изменился")
                before["candidate"] = require(repo, "versions", page_revision)["content"]
                execute(
                    conn, "DELETE FROM voice_page_drafts WHERE location_id=:id", id=location["id"]
                )
                before["candidate_proposals"] = [
                    dict(r)
                    for r in conn.execute(
                        sql_text(
                            "SELECT p.id,p.status FROM proposals p JOIN voice_candidate_proposals v ON v.proposal_id=p.id WHERE v.version_id=:version"
                        ),
                        {"version": page_revision},
                    ).mappings()
                ]
                execute(
                    conn,
                    "UPDATE voice_proposals SET revision=revision+1 WHERE proposal_id IN (SELECT proposal_id FROM voice_candidate_proposals WHERE version_id=:version)",
                    version=page_revision,
                )
                # Keep snapshots/journal, but permit a fresh comparison of discarded proposals.
                execute(
                    conn,
                    "UPDATE proposals SET status='pending' WHERE id IN (SELECT proposal_id FROM voice_candidate_proposals WHERE version_id=:version) AND status='approved'",
                    version=page_revision,
                )
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
            elif command.action in ("approve-proposal", "reject-proposal"):
                if command.proposal_id is None:
                    raise RuleViolation("Выберите предложение")
                proposal = require(repo, "proposals", str(command.proposal_id), lock=True)
                info = repo.get("voice_proposals", str(command.proposal_id), lock=True)
                if info is None:
                    if link or proposal["review_id"] != review_id:
                        raise Conflict("Предложение не относится к выбранной обработке")
                    ordinal = row(
                        conn,
                        "SELECT coalesce(max(number),-1)+1 n FROM voice_proposals WHERE review_id=:id AND run=0",
                        id=review_id,
                    )
                    info = repo.add(
                        "voice_proposals",
                        {
                            "proposal_id": proposal["id"],
                            "review_id": review_id,
                            "run": 0,
                            "number": ordinal["n"] if ordinal else 0,
                        },
                    )
                if (
                    proposal["review_id"] != review_id
                    or info["run"] != active
                    or info["revision"] != command.proposal_revision
                    or proposal["status"] != "pending"
                ):
                    raise Conflict("Предложение изменилось или относится к другой обработке")
                before["proposal"] = {
                    "proposal": copy.deepcopy(proposal),
                    "revision": info["revision"],
                }
                if command.action == "approve-proposal":
                    if review["moderation"] != "approved" or review["fraud"]:
                        raise RuleViolation("Сначала примите отзыв; фрод не применяется")
                    expected = str(command.page_revision) if command.page_revision else None
                    if expected != page_revision:
                        raise Conflict("Страница изменилась. Обновите сравнение.")
                    allowed = {"name", "cuisine", "description", "mapsUrl", "phone", "openingHours"}
                    changes = command.changes
                    if set(changes) - allowed:
                        raise RuleViolation("Неизвестное поле изменения")
                    payload = proposal["payload"]
                    target = (
                        str(command.restaurant_id)
                        if command.restaurant_id
                        else payload.get("restaurantId")
                    )
                    if (
                        candidate
                        and (str(candidate["base_version"]) if candidate["base_version"] else None)
                        != location["published_version"]
                    ):
                        raise Conflict(
                            "Страница изменилась после подготовки правок. Сначала сбросьте черновик."
                        )
                    content = (
                        copy.deepcopy(require(repo, "versions", page_revision)["content"])
                        if candidate
                        else current_content(repo, location)
                    )
                    current = next(
                        (r for r in content["restaurants"] if r.get("id") == target), None
                    )
                    if proposal["kind"] == "edit" and current is None:
                        raise RuleViolation("Выберите ресторан текущей страницы для правки")
                    original = {
                        k: v
                        for k, v in (current or {}).items()
                        if k in RestaurantInput.model_fields
                    }
                    final = {**original, **changes}
                    if final.get("phone") is None:
                        final["phone"] = ""
                    validated = RestaurantInput.model_validate(final).model_dump(mode="json")
                    if current is None:
                        if not validated["phone"]:
                            raise RuleViolation(
                                "Заполните телефон; предложение остаётся сохранённым"
                            )
                        content["restaurants"].append(validated)
                    else:
                        current.update(validated)
                    version = str(uuid4())
                    repo.add(
                        "versions",
                        {
                            "id": version,
                            "location_id": location["id"],
                            "content": content,
                            "source": "voice_candidate",
                            "number": repo.next_version(location["id"]),
                            "published": 0,
                        },
                    )
                    if candidate:
                        repo.inherit_reviews(page_revision, version)
                        execute(
                            conn,
                            "INSERT INTO voice_candidate_proposals(version_id,proposal_id) SELECT :version,proposal_id FROM voice_candidate_proposals WHERE version_id=:previous",
                            version=version,
                            previous=page_revision,
                        )
                    execute(
                        conn,
                        "INSERT INTO voice_candidate_proposals(version_id,proposal_id) VALUES(:version,:proposal)",
                        version=version,
                        proposal=proposal["id"],
                    )
                    execute(
                        conn,
                        "INSERT INTO voice_page_drafts(location_id,version_id,base_version) VALUES(:id,:version,:base) ON CONFLICT(location_id) DO UPDATE SET version_id=excluded.version_id",
                        id=location["id"],
                        version=version,
                        base=location["published_version"],
                    )
                    repo.link("version_reviews", {"version_id": version, "review_id": review_id})
                    result.update(version_id=version, changes=changes)
                repo.change(
                    "proposals",
                    proposal["id"],
                    {"status": "approved" if command.action == "approve-proposal" else "rejected"},
                )
                repo.change("voice_proposals", proposal["id"], {"revision": info["revision"] + 1})
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
            return result
