import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from involta_guide.application.commands.schemas import (
    ContentInput,
    Generation,
)
from involta_guide.application.ports.repositories import (
    AIProvider,
    Record,
    Repository,
    UowFactory,
)
from involta_guide.domain.entities import Conflict, RuleViolation
from involta_guide.domain.rules import assert_publishable, rank

from .editorial import current_content, require, save
from .generation_quality import (
    CANDIDATE_LIMIT,
    assess_coverage,
    normalize_generation,
    prepare_generation_prompt,
)


class JobService:
    def __init__(
        self,
        uow: UowFactory,
        provider: AIProvider,
        *,
        public_fill_enabled: bool = True,
    ) -> None:
        self.uow = uow
        self.provider = provider
        self.public_fill_enabled = public_fill_enabled
        self.reset_handler: Callable[[Record], None] | None = None

    def generation_model(self) -> str:
        with self.uow() as uow:
            return uow.repo.setting("generation_model", self.provider.model)

    @staticmethod
    def _model(value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
            raise RuleViolation("Укажите идентификатор модели OpenAI, например gpt-5")
        return value

    def set_generation_model(self, value: str) -> None:
        value = self._model(value)
        with self.uow() as uow:
            uow.repo.set_setting("generation_model", value)
            uow.repo.audit("generation_model", "generation", value)

    def save_filling_settings(self, prompt: str, model: str) -> None:
        if not 50 <= len(prompt) <= 30000 or "\x00" in prompt:
            raise RuleViolation("Промпт: 50–30000 символов")
        model = self._model(model)
        with self.uow() as uow:
            uow.repo.set_setting("prompt", prompt)
            uow.repo.set_setting("generation_model", model)
            uow.repo.audit("filling_settings", "generation", json.dumps({"model": model}))

    @staticmethod
    def generation_config(repo: Repository, job_id: str) -> Record:
        job = require(repo, "jobs", job_id)
        return {
            "model": job["model"],
            "baseline_revision": job["baseline_revision"],
            "content_epoch": job["content_epoch"],
        }

    def _queue_generation(
        self,
        repo: Repository,
        location: Record,
        *,
        model: str | None = None,
    ) -> str | None:
        # The DB assigns the canonical city/number key atomically for every request.
        # Read prompt/model in one statement so concurrent settings saves cannot mix the pair.
        settings = {row["key"]: row["value"] for row in repo.rows("settings", limit=1000)}
        chosen = self._model(model or settings.get("generation_model", self.provider.model))
        job_id = str(uuid4())
        if not repo.queue(
            {
                "id": job_id,
                "kind": "generate",
                "location_id": location["id"],
                "prompt": settings.get("prompt", ""),
                "model": chosen,
                "baseline_revision": location["revision"],
                "content_epoch": location["content_epoch"],
                "public_log": True,
            }
        ):
            return None
        return job_id

    @staticmethod
    def _has_data(repo: Repository, location: Record) -> bool:
        return location["initialized_at"] is not None

    def _public_state(self, repo: Repository, location: Record) -> Record:
        if location["is_hub"]:
            return {"state": "hub", "can_start": False, "message": "Выберите подлокацию."}
        if location["reset_pending"]:
            return {"state": "resetting", "can_start": False, "message": "Сброс не завершён."}
        if self._has_data(repo, location):
            return {"state": "ready", "can_start": False, "message": "Подборка готова."}
        filters = {"kind": "generate", "location_id": location["id"]}
        for status, message in (
            ("running", "Ищем и проверяем места. Это может занять несколько минут."),
            ("queued", "Запрос принят. Результат появится на этой странице."),
        ):
            if repo.rows("jobs", {**filters, "status": status}, limit=1):
                return {"state": status, "can_start": False, "message": message}
        latest = repo.rows("jobs", filters, limit=1)
        if latest:
            return {
                "state": "failed",
                "can_start": False,
                "message": "Первое наполнение не завершено. Повтор доступен администратору в логе. "
                + (latest[0]["error"] or "Попробуйте ещё раз.")[:500],
            }
        return {
            "state": "empty",
            "can_start": True,
            "message": "Найдём места, которые стоит посетить. Поиск может занять несколько минут.",
        }

    def public_fill_state(self, location_id: str) -> Record:
        with self.uow() as uow:
            location = require(uow.repo, "locations", location_id)
            return self._public_state(uow.repo, location)

    def enqueue_public_fill(self, location_id: str) -> Record:
        """TEMPORARY authless initial fill; never replace existing editorial data."""
        if not self.public_fill_enabled:
            raise RuleViolation("Временное наполнение отключено")
        with self.uow() as uow:
            repo = uow.repo
            repo.health()
            location = require(repo, "locations", location_id, lock=True)
            if not location["active"] or location["is_hub"] or location["reset_pending"]:
                raise RuleViolation("Локация отключена")
            state = self._public_state(repo, location)
            if not state["can_start"]:
                return state
            self._queue_generation(repo, location)
            return self._public_state(repo, location)

    def regenerate(self, job_id: str, *, model: str | None = None) -> str:
        with self.uow() as uow:
            uow.repo.health()
            old = require(uow.repo, "jobs", job_id)
            if old["kind"] != "generate":
                raise RuleViolation("Новый запрос доступен только для наполнения")
            location = require(uow.repo, "locations", old["location_id"], lock=True)
            if not location["active"] or location["is_hub"] or location["reset_pending"]:
                raise RuleViolation("Локация отключена")
            if (
                location["initialized_at"] is not None
                or old["content_epoch"] != location["content_epoch"]
            ):
                raise Conflict("Подборка уже наполнена; доступен только полный бета-сброс")
            new_id = self._queue_generation(uow.repo, location, model=model)
            if not new_id:
                raise Conflict("Для этой локации уже есть запрос в очереди или в работе")
            return new_id

    def set_prompt(self, prompt: str) -> None:
        if not 50 <= len(prompt) <= 30000:
            raise RuleViolation("Промпт: 50–30000 символов")
        with self.uow() as uow:
            uow.repo.set_setting("prompt", prompt)
            uow.repo.audit("prompt", "generation")

    def process(self, job: Record) -> None:
        if job["kind"] == "reset":
            # The worker attaches its infrastructure reset coordinator, outside HTTP.
            if self.reset_handler is None:
                raise RuleViolation("Координатор сброса не настроен")
            self.reset_handler(job)
            return
        with self.uow() as uow:
            location = require(uow.repo, "locations", job["location_id"])
            if (
                location["content_epoch"] != job["content_epoch"]
                or location["reset_pending"]
                or location["is_hub"]
                or location["initialized_at"] is not None
            ):
                raise Conflict("Запуск больше не относится к текущему пустому циклу")
            baseline = job["baseline_revision"]
            saved_prompt = uow.repo.generation_prompt(job["id"])
            content = current_content(uow.repo, location)
            known = uow.repo.known(location["id"])
            parents = uow.repo.tree(location["id"], ancestors=True)
            baseline_changed = location["revision"] != baseline
        if job["kind"] == "generate":
            scope = (
                location["coverage_note"]
                or "Только " + location["name"] + ", " + location["region"]
            )
            replacements = {
                "{локация}": location["name"],
                "{город}": location["name"],
                "{регион}": location["region"],
                "{лимит}": str(CANDIDATE_LIMIT),
                "{родитель}": parents[-1]["name"] if parents else "нет",
                "{охват}": scope,
            }
            prompt = prepare_generation_prompt(job["prompt"] or "")
            for key, value in replacements.items():
                prompt = prompt.replace(key, value)
            prompt += "\nОБЯЗАТЕЛЬНЫЙ ГЕОГРАФИЧЕСКИЙ КОНТЕКСТ\n" + json.dumps(
                {
                    "location": location["name"],
                    "scope": scope,
                    "parent": parents,
                    "knownRestaurants": known,
                    "candidateLimit": CANDIDATE_LIMIT,
                    "previousCandidates": content.get("candidateReview") or [],
                },
                default=str,
                ensure_ascii=False,
            )
            prompt = saved_prompt or prompt
            # Nullable ranking fields are required keys at the ContentInput boundary.
            normalized, normalization = normalize_generation(
                self.provider.generate(prompt, job["id"])
            )
            generated = Generation.model_validate(normalized).model_dump(mode="json")
            # Finish retrieving an already-paid response even if an editor changed the page.
            # Save it for audit, but never overwrite the editor's new version.
            if baseline_changed:
                raise Conflict(
                    "Ответ сохранён, но страница изменена после постановки запроса. Автоматическое применение отменено."
                )
            result = assess_coverage(rank(generated), normalization)
            ContentInput.model_validate(result)
            assert_publishable(result)
            with self.uow() as uow:
                repo = uow.repo
                if not repo.lease_valid(job["id"], job["lease_owner"]):
                    raise Conflict("Worker lease lost")
                revision = save(
                    repo,
                    location["id"],
                    result,
                    baseline,
                    job_id=job["id"],
                )
                repo.audit(
                    "ai_trace", job["id"], json.dumps({"event": "response_validated", "data": {}})
                )
                repo.audit(
                    "generation_quality",
                    job["id"],
                    json.dumps(
                        {
                            "event": "generation_quality",
                            "data": {
                                **result["qualityReport"],
                                "candidateReview": result.get("candidateReview", []),
                            },
                        },
                        ensure_ascii=False,
                    ),
                )
                repo.audit(
                    "ai_result",
                    job["id"],
                    json.dumps(
                        {
                            "event": "result_saved",
                            "data": {"revision": revision},
                        }
                    ),
                )
                if result.get("warnings"):
                    repo.audit("generation_warnings", location["id"], "\n".join(result["warnings"]))
                self._done(repo, job)
            return

    @staticmethod
    def _done(repo: Repository, job: Record) -> None:
        # Executed in the same transaction as the result, fenced by lease_valid.
        repo.change(
            "jobs",
            job["id"],
            {
                "status": "done",
                "applied": True,
                "finished_at": datetime.now(UTC),
                "error": None,
                "lease_until": None,
                "lease_owner": None,
            },
        )
