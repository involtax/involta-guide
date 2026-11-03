"""The beta's one owner-local reset coordinator, using existing PostgreSQL jobs.

Two databases/files have no shared commit. The location stays closed until every
external step is confirmed. Repeating the same reset key resumes the same epoch.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from involta_guide.application.services.editorial import require
from involta_guide.application.services.jobs import JobService
from involta_guide.config import Settings
from involta_guide.domain.entities import Conflict, RuleViolation
from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow
from involta_guide.infrastructure.storage.audio import FileAudioStorage
from involta_guide.infrastructure.voice.client import VoiceClient
from involta_voice.db import execute


class BetaReset:
    def __init__(
        self, engine: Engine, settings: Settings, jobs: JobService, client: Any = None
    ) -> None:
        self.engine, self.settings, self.jobs = engine, settings, jobs
        self.client = client or VoiceClient(settings)

    def begin(self, location_id: str, key: str, expected_epoch: int, confirmation: str) -> str:
        if not self.settings.beta_reset_enabled:
            raise RuleViolation("Бета-сброс отключён")
        key = str(UUID(key))
        with PostgresUow(self.engine) as uow:
            repo = uow.repo
            location = require(repo, "locations", location_id, lock=True)
            if location["is_hub"]:
                raise RuleViolation("У надлокации нет собственной подборки")
            if location["reset_key"] == key:
                job = repo.get("jobs", key, lock=True)
                if job and job["status"] == "failed" and location["reset_pending"]:
                    repo.change(
                        "jobs",
                        key,
                        {
                            "status": "queued",
                            "attempts": 0,
                            "error": None,
                            "available_at": datetime.now(UTC),
                        },
                    )
                return key
            if location["reset_pending"]:
                return location["reset_key"]
            if location["content_epoch"] != expected_epoch:
                raise Conflict("Цикл изменён; обновите карточку локации")
            if confirmation != location["name"]:
                raise RuleViolation("Введите название локации для подтверждения полного удаления")
            epoch = location["content_epoch"] + 1
            repo.change(
                "locations",
                location_id,
                {
                    "content_epoch": epoch,
                    "reset_pending": True,
                    "reset_key": key,
                    "reset_error": None,
                    "revision": str(uuid4()),
                },
            )
            repo.add(
                "jobs",
                {
                    "id": key,
                    "kind": "reset",
                    "location_id": location_id,
                    "content_epoch": epoch,
                    "public_log": False,
                },
            )
            return key

    def cancel_previous(self, location_id: str) -> None:
        cancel = getattr(self.jobs.provider, "cancel_generation", None)
        if not cancel:
            return
        with self.engine.connect() as conn:
            responses = (
                conn.execute(
                    text("""SELECT g.response_id FROM generation_requests g
                JOIN jobs j ON j.id=g.job_id WHERE j.location_id=:id
                AND g.response_id IS NOT NULL AND g.state IN ('queued','in_progress')"""),
                    {"id": location_id},
                )
                .scalars()
                .all()
            )
        for response_id in responses:
            cancel(response_id)

    def perform(self, job: dict[str, Any]) -> None:
        if not self.settings.beta_reset_enabled:
            raise RuleViolation("Бета-сброс отключён; незавершённая очистка сохранена")
        location_id, epoch = job["location_id"], job["content_epoch"]
        with PostgresUow(self.engine) as uow:
            location = require(uow.repo, "locations", location_id, lock=True)
            if location["reset_key"] != job["id"] or epoch != location["content_epoch"]:
                raise Conflict("Устаревший сброс")
            if not location["reset_pending"]:
                return
            if not uow.repo.lease_valid(job["id"], job["lease_owner"]):
                raise Conflict("Worker lease lost")
        self.cancel_previous(location_id)
        result = self.client.json(
            "POST", "/locations/" + location_id + "/reset", json={"content_epoch": epoch}
        )
        if result.get("purged") is not True or result.get("content_epoch") != epoch:
            raise RuleViolation("Voice не подтвердил полное удаление")
        cleanup_files(self.engine, self.settings, location_id)
        with PostgresUow(self.engine) as uow:
            repo, conn = uow.repo, uow.session.connection()
            location = require(repo, "locations", location_id, lock=True)
            if location["content_epoch"] != epoch or location["reset_key"] != job["id"]:
                raise Conflict("Цикл изменён")
            if not location["reset_pending"]:
                return
            if not repo.lease_valid(job["id"], job["lease_owner"]):
                raise Conflict("Worker lease lost")
            # Explicit FK order. Other locations, global cursors/sequences/settings are untouched.
            for table in [
                "review_fragment_bindings",
                "voice_proposals",
                "voice_fragments",
                "voice_results",
                "review_decisions",
                "voice_links",
                "review_texts",
            ]:
                execute(
                    conn,
                    f"DELETE FROM {table} WHERE review_id IN (SELECT id FROM reviews WHERE location_id=:id)",
                    id=location_id,
                )
            execute(conn, "DELETE FROM voice_inbox WHERE location_id=:id", id=location_id)
            execute(conn, "DELETE FROM proposals WHERE location_id=:id", id=location_id)
            execute(
                conn,
                "DELETE FROM audit WHERE location_id=:id OR target=:target OR target IN (SELECT id::text FROM jobs WHERE location_id=:id) OR target IN (SELECT id::text FROM reviews WHERE location_id=:id)",
                id=location_id,
                target=location_id,
                job=job["id"],
            )
            execute(
                conn,
                "DELETE FROM generation_requests WHERE job_id IN (SELECT id FROM jobs WHERE location_id=:id AND id<>:job)",
                id=location_id,
                job=job["id"],
            )
            execute(
                conn,
                "DELETE FROM ai_usage WHERE job_id IN (SELECT id FROM jobs WHERE location_id=:id AND id<>:job)",
                id=location_id,
                job=job["id"],
            )
            execute(
                conn,
                "DELETE FROM request_logs WHERE job_id IN (SELECT id FROM jobs WHERE location_id=:id AND id<>:job)",
                id=location_id,
                job=job["id"],
            )
            execute(
                conn,
                "DELETE FROM jobs WHERE location_id=:id AND id<>:job",
                id=location_id,
                job=job["id"],
            )
            execute(conn, "DELETE FROM reviews WHERE location_id=:id", id=location_id)
            execute(conn, "DELETE FROM restaurants WHERE location_id=:id", id=location_id)
            execute(
                conn, "DELETE FROM beta_transition_report WHERE location_id=:id", id=location_id
            )
            values = {
                "intro": "",
                "initialized_at": None,
                "reset_pending": False,
                "reset_error": None,
                "revision": str(uuid4()),
                "updated_at": datetime.now(UTC),
            }
            repo.change("locations", location_id, values)
            queued = self.jobs._queue_generation(repo, {**location, **values})
            if not queued:
                raise Conflict("Новый первоначальный запуск не создан")
            execute(conn, "DELETE FROM jobs WHERE id=:id", id=job["id"])


def cleanup_files(engine: Engine, settings: Settings, location_id: str | None = None) -> None:
    storage = FileAudioStorage(settings.data_dir, settings.max_audio_bytes)
    with engine.connect() as conn:
        paths = (
            conn.execute(
                text(
                    "SELECT path FROM beta_cleanup_files WHERE CAST(:id AS uuid) IS NULL OR location_id=CAST(:id AS uuid)"
                ),
                {"id": location_id},
            )
            .scalars()
            .all()
        )
    for path in paths:
        storage.delete(path)
        with engine.begin() as conn:
            execute(conn, "DELETE FROM beta_cleanup_files WHERE path=:path", path=path)
