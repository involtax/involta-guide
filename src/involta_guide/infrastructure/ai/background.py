"""One durable POST per generation job, followed only by resumable GETs."""

import json
import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NoReturn

import httpx
from sqlalchemy import Engine, text

from involta_guide.application.ports.repositories import Record
from involta_guide.domain.entities import Conflict, RuleViolation
from involta_guide.infrastructure.ai.errors import GenerationPending, GenerationStopped

if TYPE_CHECKING:
    from involta_guide.infrastructure.ai.openai import OpenAIProvider


UNKNOWN = (
    "Неизвестно, принял ли OpenAI запрос: идентификатор ответа не сохранён. "
    "Автоматическая повторная генерация остановлена. Проверьте журнал перед новым запросом."
)


class BackgroundGeneration:
    def __init__(self, provider: "OpenAIProvider") -> None:
        self.provider = provider
        self.engine = provider.engine
        self.settings = provider.settings
        self.audit = provider.audit

    def advance(self, job_id: str, body: Record) -> Record:
        # Reserve usage and mark submission in one transaction BEFORE any network activity.
        # This intentionally favours an explicit unknown outcome over duplicate paid requests.
        with self.engine.begin() as conn:
            live = conn.execute(
                text(
                    "SELECT l.id FROM locations l JOIN jobs j ON j.location_id=l.id WHERE j.id=:id AND j.content_epoch=l.content_epoch AND NOT l.reset_pending AND l.initialized_at IS NULL FOR SHARE OF l"
                ),
                {"id": job_id},
            ).first()
            if not live:
                raise Conflict("Цикл наполнения завершён или сброшен")
            conn.execute(text("SELECT pg_advisory_xact_lock(47031009)"))
            row = (
                conn.execute(
                    text("SELECT * FROM generation_requests WHERE job_id=:id"), {"id": job_id}
                )
                .mappings()
                .first()
            )
            fresh = row is None
            if fresh:
                # Old foreground work interrupted during an upgrade cannot safely be resubmitted.
                previous = conn.execute(
                    text(
                        "SELECT 1 FROM ai_usage WHERE job_id=:id AND operation='responses' LIMIT 1"
                    ),
                    {"id": job_id},
                ).first()
                if previous:
                    raise GenerationStopped(
                        "Предыдущий запрос выполнялся в старом режиме. "
                        "Проверьте его лог; автоматическая повторная отправка остановлена."
                    )
                usage_id = self.provider._reserve(job_id, "responses", body["model"], conn)
                row = (
                    conn.execute(
                        text("""INSERT INTO generation_requests(job_id,usage_id,request_json,deadline_at)
                    VALUES(:id,:usage,CAST(:body AS jsonb),now()+make_interval(secs=>:wait))
                    RETURNING *"""),
                        {
                            "id": job_id,
                            "usage": usage_id,
                            "body": json.dumps(self.audit.clean(body), ensure_ascii=False),
                            "wait": self.settings.generation_wait_seconds,
                        },
                    )
                    .mappings()
                    .one()
                )
            assert row is not None
            task = dict(row)

        if not fresh:
            body = task["request_json"]

        if task["state"] == "completed":
            return dict(task["response_json"])
        if not fresh and not task["response_id"]:
            if task["state"] in ("submitting", "unknown"):
                self._stop(task, "unknown", UNKNOWN)
            raise GenerationStopped(task["last_error"] or "OpenAI отклонил запрос; см. полный лог")
        if task["state"] in ("failed", "cancelled", "incomplete", "unavailable"):
            raise GenerationStopped(task["last_error"] or "Генерация завершилась без результата")
        if task["deadline_at"] <= datetime.now(UTC):
            self._stop(
                task,
                "paused",
                "Истёк срок ожидания. Запрос OpenAI сохранён; можно продолжить проверку того же ответа.",
            )

        method = "POST" if fresh else "GET"
        path = "responses" if fresh else "responses/" + task["response_id"]
        url = "https://api.openai.com/v1/" + path
        self.audit.record(
            job_id,
            "request",
            {
                "method": method,
                "url": url,
                "model": body["model"],
                "json": body if fresh else None,
                "response_id": task["response_id"],
            },
            call_id=str(task["usage_id"]),
        )
        started = time.monotonic()
        try:
            response = self.provider.client.request(
                method,
                url,
                headers={
                    "Authorization": "Bearer " + self.settings.openai_api_key.get_secret_value()
                },
                json=body if fresh else None,
                timeout=httpx.Timeout(60, connect=15),
            )
        except httpx.HTTPError as exc:
            self.audit.record(
                job_id,
                "provider_error",
                {
                    "method": method,
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "response_id": task["response_id"],
                },
                call_id=str(task["usage_id"]),
            )
            if fresh:
                self._stop(task, "unknown", UNKNOWN)
            self._pending(
                task, "Временно не удалось проверить OpenAI. Повторим проверку того же ответа."
            )

        # Persist every HTTP response, including errors and all intermediate snapshots.
        self.audit.record(
            job_id,
            "response",
            {
                "method": method,
                "status": response.status_code,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "headers": dict(response.headers),
                "body": response.text,
            },
            call_id=str(task["usage_id"]),
        )
        if response.status_code >= 400:
            from involta_guide.infrastructure.ai.openai import provider_error

            message = provider_error(response)
            if fresh:
                self._stop(
                    task,
                    "unknown" if response.status_code >= 500 else "failed",
                    message + (" " + UNKNOWN if response.status_code >= 500 else ""),
                )
            if response.status_code in (408, 409, 429) or response.status_code >= 500:
                self._pending(task, message + ". Повторим проверку того же ответа.")
            self._stop(task, "unavailable", message + ". Идентификатор ответа сохранён.")
        try:
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("Expected response object")
        except ValueError:
            if fresh:
                self._stop(task, "unknown", UNKNOWN)
            self._pending(task, "OpenAI вернул нечитаемый ответ статуса. Повторим проверку.")
        response_id = result.get("id", "")
        if not isinstance(response_id, str) or not re.fullmatch(
            r"resp_[A-Za-z0-9_-]{1,200}", response_id
        ):
            if fresh:
                self._stop(task, "unknown", UNKNOWN)
            self._pending(
                task, "В ответе OpenAI отсутствует корректный идентификатор. Повторим проверку."
            )
        if task["response_id"] and response_id != task["response_id"]:
            self._stop(
                task,
                "unavailable",
                "OpenAI вернул другой идентификатор ответа. Применение остановлено.",
            )
        state = result.get("status")
        if state not in ("queued", "in_progress", "completed", "failed", "cancelled", "incomplete"):
            self._stop(
                task,
                "unknown" if fresh else "unavailable",
                "Неизвестное состояние OpenAI; полный ответ сохранён в логе.",
            )

        # Snapshot + usage are atomic; validation happens only after this commit.
        usage = result.get("usage") or {}
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        prices = json.loads(self.settings.ai_prices_json).get(body["model"])
        cost = (
            (input_tokens * prices.get("input", 0) + output_tokens * prices.get("output", 0))
            / 1_000_000
            if prices and usage
            else None
        )
        failure = (
            None
            if state in ("queued", "in_progress", "completed")
            else (
                "OpenAI завершил генерацию: "
                + state
                + ". "
                + json.dumps(
                    self.audit.clean(result.get("error") or result.get("incomplete_details") or {}),
                    ensure_ascii=False,
                )
            )
        )
        with self.engine.begin() as conn:
            conn.execute(
                text("""UPDATE generation_requests SET response_id=:response,state=:state,
                response_json=CAST(:body AS jsonb),last_error=:error,updated_at=now(),
                poll_count=poll_count+:poll WHERE job_id=:id"""),
                {
                    "id": job_id,
                    "response": response_id,
                    "state": state,
                    "body": json.dumps(self.audit.clean(result), ensure_ascii=False),
                    "error": failure,
                    "poll": 0 if fresh else 1,
                },
            )
            conn.execute(
                text("""UPDATE ai_usage SET status=:status,input_tokens=:input,
                output_tokens=:output,estimated_cost_usd=:cost,detail=:detail WHERE id=:id"""),
                {
                    "id": task["usage_id"],
                    "status": "done"
                    if state == "completed"
                    else "reserved"
                    if state in ("queued", "in_progress")
                    else "failed",
                    "input": input_tokens,
                    "output": output_tokens,
                    "cost": cost,
                    "detail": failure or "",
                },
            )
        if state != task["state"]:
            self.audit.record(
                job_id, "generation_status", {"state": state, "response_id": response_id}
            )
        if state == "completed":
            return result
        if failure:
            raise GenerationStopped(failure)
        raise GenerationPending()

    def _stop(self, task: Record, state: str, message: str) -> NoReturn:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE generation_requests SET state=:state,last_error=:error,updated_at=now() WHERE job_id=:id"
                ),
                {"state": state, "error": message, "id": task["job_id"]},
            )
            conn.execute(
                text("UPDATE ai_usage SET status=:state,detail=:error WHERE id=:id"),
                {
                    "state": "failed" if state == "failed" else "reserved",
                    "error": message,
                    "id": task["usage_id"],
                },
            )
        raise GenerationStopped(message)

    def _pending(self, task: Record, message: str) -> NoReturn:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE generation_requests SET last_error=:error,poll_count=poll_count+1,updated_at=now() WHERE job_id=:id"
                ),
                {"error": message, "id": task["job_id"]},
            )
        raise GenerationPending(message)


def resume_generation(
    engine: Engine,
    job_id: str,
    wait_seconds: int,
    *,
    reprocess: bool = False,
    expected_version: str | None = None,
) -> None:
    """Explicitly resume GETs or revalidate a cached result, never create another POST."""
    with engine.begin() as conn:
        location_id = conn.execute(
            text("SELECT location_id FROM jobs WHERE id=:id"), {"id": job_id}
        ).scalar_one()
        location = (
            conn.execute(
                text("SELECT * FROM locations WHERE id=:id FOR UPDATE"), {"id": location_id}
            )
            .mappings()
            .one()
        )
        job = (
            conn.execute(text("SELECT * FROM jobs WHERE id=:id FOR UPDATE"), {"id": job_id})
            .mappings()
            .one()
        )
        task = (
            conn.execute(
                text("SELECT * FROM generation_requests WHERE job_id=:id FOR UPDATE"),
                {"id": job_id},
            )
            .mappings()
            .first()
        )
        if (
            not task
            or not task["response_id"]
            or task["state"] not in ("completed", "paused", "unavailable", "queued", "in_progress")
        ):
            raise RuleViolation(
                "Нет сохранённого ответа, который можно продолжить или проверить повторно"
            )
        if (
            location["is_hub"]
            or location["reset_pending"]
            or location["initialized_at"] is not None
            or job["content_epoch"] != location["content_epoch"]
        ):
            raise Conflict("Подборка уже наполнена или цикл изменён; доступен полный бета-сброс")
        if job["applied"] or job["status"] not in ("failed", "cancelled"):
            raise Conflict("Запуск уже применён или ещё выполняется")
        if (
            reprocess
            and (str(location["revision"]) if location["revision"] else None) != expected_version
        ):
            raise Conflict("Страница изменилась")
        active = conn.execute(
            text(
                "SELECT 1 FROM jobs WHERE location_id=:location AND kind='generate' AND status IN ('queued','running')"
            ),
            {"location": location_id},
        ).first()
        if active:
            raise Conflict("Для этой локации уже выполняется другой запрос")
        conn.execute(
            text("""UPDATE generation_requests SET state=CASE WHEN state='completed' THEN state ELSE 'in_progress' END,
            last_error=NULL,deadline_at=now()+make_interval(secs=>:wait),updated_at=now() WHERE job_id=:id"""),
            {"id": job_id, "wait": wait_seconds},
        )
        conn.execute(
            text(
                "UPDATE jobs SET status='queued',applied=false,attempts=0,error=NULL,finished_at=NULL,available_at=now(),lease_owner=NULL,lease_until=NULL WHERE id=:id"
            ),
            {"id": job_id},
        )
