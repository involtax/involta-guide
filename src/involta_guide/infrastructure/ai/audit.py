"""Durable per-job AI traces in the existing PostgreSQL audit journal."""

import json
import re
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from pydantic import SecretStr
from sqlalchemy import Engine, text

from involta_guide.config import Settings

HISTORY = """WITH history AS (
 SELECT j.*,l.name location_name,r.location_slug,r.number run_number,r.path log_path,
 g.state generation_state,g.response_id,g.poll_count,g.last_error generation_error,
 (g.response_json IS NOT NULL) has_saved_response,(j.report->'qualityReport') || jsonb_build_object('candidateReview',coalesce(j.report->'candidateReview','[]'::jsonb)) quality_report,
 j.applied result_published,
 jsonb_build_object('model',j.model,'public_log',j.public_log,'baseline_revision',j.baseline_revision) generation_config,
 j.model model_name,l.initialized_at,l.content_epoch current_epoch,l.reset_pending
 FROM jobs j JOIN request_logs r ON r.job_id=j.id JOIN locations l ON l.slug=r.location_slug
 LEFT JOIN generation_requests g ON g.job_id=j.id
) """
PUBLIC_GENERATION = "j.kind='generate' AND j.public_log"


class AIAudit:
    def __init__(self, engine: Engine, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings
        self.secrets = {
            value.get_secret_value()
            for name in type(settings).model_fields
            if isinstance(value := getattr(settings, name), SecretStr) and value.get_secret_value()
        }
        db_password = urlsplit(settings.database_url.get_secret_value()).password
        if db_password:
            self.secrets.add(unquote(db_password))

    def clean(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(k): "[REDACTED]"
                if str(k).lower()
                in {
                    "authorization",
                    "cookie",
                    "set-cookie",
                    "api_key",
                    "password",
                    "admin_password_hash",
                    "session_secret",
                    "token_hash",
                    "csrf_hash",
                }
                else self.clean(v)
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.clean(v) for v in value]
        if isinstance(value, str):
            for secret in sorted(self.secrets, key=len, reverse=True):
                if len(secret) >= 8:
                    value = value.replace(secret, "[REDACTED]")
                else:
                    value = re.sub(r"(?<!\w)" + re.escape(secret) + r"(?!\w)", "[REDACTED]", value)
            value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[REDACTED]", value)
            return re.sub(r"(postgres(?:ql)?(?:\+psycopg)?://)[^\s@]+@", r"\1[REDACTED]@", value)
        return value

    def record(self, job_id: str, event: str, data: Any, *, call_id: str | None = None) -> None:
        payload = self.clean(
            {"event": event, "call_id": call_id, "release": self.settings.release_sha, "data": data}
        )
        with self.engine.begin() as conn:
            valid = conn.execute(
                text(
                    "SELECT l.id FROM locations l JOIN jobs j ON j.location_id=l.id WHERE j.id=:id AND j.content_epoch=l.content_epoch AND (NOT l.reset_pending OR j.kind='reset') FOR SHARE OF l"
                ),
                {"id": job_id},
            ).first()
            if not valid:
                return
            conn.execute(
                text(
                    "INSERT INTO audit(id,action,target,detail) VALUES(:id,'ai_trace',:job,:detail)"
                ),
                {
                    "id": str(uuid4()),
                    "job": job_id,
                    "detail": json.dumps(payload, ensure_ascii=False, default=str),
                },
            )

    def listing(
        self, page: int, query: str = "", status: str = "", *, public_only: bool = False
    ) -> dict[str, Any]:
        where = "WHERE (:q='' OR j.location_name ILIKE :search OR j.location_slug=:q OR (j.location_slug||'/'||j.run_number::text)=:q) AND (:status='' OR j.status=:status)"
        if public_only:
            where += " AND " + PUBLIC_GENERATION
        params = {"q": query, "search": "%" + query + "%", "status": status}
        with self.engine.connect() as conn:
            total = conn.execute(
                text(HISTORY + "SELECT count(*) FROM history j " + where),
                params,
            ).scalar_one()
            pages = max(1, (total + 39) // 40)
            page = min(max(1, page), pages)
            rows = (
                conn.execute(
                    text(
                        HISTORY
                        + "SELECT j.*,(SELECT count(*) FROM ai_usage u WHERE u.job_id=j.id) calls "
                        "FROM history j "
                        + where
                        + " ORDER BY j.created_at DESC,j.id DESC LIMIT 40 OFFSET :offset"
                    ),
                    {**params, "offset": (page - 1) * 40},
                )
                .mappings()
                .all()
            )
        return self.clean(
            {
                "rows": [self._job(dict(r)) for r in rows],
                "total": total,
                "page": page,
                "pages": pages,
            }
        )

    @staticmethod
    def _job(job: dict[str, Any]) -> dict[str, Any]:
        state = job.get("generation_state")
        labels = {
            "queued": "В очереди",
            "running": "Обрабатывается",
            "failed": "Ошибка",
            "done": "Готово",
            "cancelled": "Остановлено",
        }
        job["stage_label"] = labels.get(job["status"], job["status"])
        if job["kind"] == "generate":
            if job["status"] == "done":
                job["stage_label"] = (
                    "Опубликовано"
                    if job.get("result_published")
                    else "Изменения применены; страница обновлялась позже"
                )
            elif job["status"] != "cancelled" and state:
                job["stage_label"] = {
                    "submitting": "Отправляется в OpenAI",
                    "queued": "В очереди OpenAI",
                    "in_progress": "OpenAI выполняет запрос",
                    "unknown": "Исход отправки неизвестен",
                    "paused": "Ожидание приостановлено; запрос сохранён",
                    "unavailable": "Не удалось получить сохранённый ответ",
                    "failed": "OpenAI отклонил или завершил запрос с ошибкой",
                    "cancelled": "OpenAI отменил запрос",
                    "incomplete": "OpenAI вернул незавершённый ответ",
                    "completed": "Ответ сохранён; проверка или применение не прошли"
                    if job["status"] == "failed"
                    else "Ответ сохранён; проверяется",
                }.get(state, job["stage_label"])
                if job["status"] == "failed" and state in ("queued", "in_progress"):
                    job["stage_label"] = "Обработка остановлена; запрос OpenAI сохранён"
        if job["status"] == "done" and (job.get("quality_report") or {}).get("status") == "partial":
            job["stage_label"] = "Поиск неполный"
        job["can_reprocess"] = (
            job["kind"] == "generate"
            and job["status"] in ("failed", "cancelled")
            and not job.get("initialized_at")
            and not job.get("reset_pending")
            and job.get("content_epoch") == job.get("current_epoch")
            and state == "completed"
            and bool(job.get("has_saved_response"))
        )
        job["can_resume"] = (
            job["status"] == "failed"
            and not job.get("initialized_at")
            and not job.get("reset_pending")
            and job.get("content_epoch") == job.get("current_epoch")
            and not job["applied"]
            and bool(job.get("response_id"))
            and state in ("completed", "paused", "unavailable", "queued", "in_progress")
        )
        return job

    def resolve(self, slug: str, number: int, *, public_only: bool = False) -> str | None:
        with self.engine.connect() as conn:
            value = conn.execute(
                text(
                    HISTORY
                    + "SELECT j.id FROM history j WHERE (j.location_slug=:slug OR j.location_id IN (SELECT location_id FROM location_slug_redirects WHERE slug=:slug)) AND j.run_number=:number"
                    + (" AND " + PUBLIC_GENERATION if public_only else "")
                ),
                {"slug": slug, "number": number},
            ).scalar_one_or_none()
        return str(value) if value else None

    def status(self, slug: str, number: int, *, public_only: bool = False) -> dict[str, Any] | None:
        job_id = self.resolve(slug, number, public_only=public_only)
        if not job_id:
            return None
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        HISTORY
                        + """SELECT j.kind,j.status,j.error,j.applied,
                j.generation_state,j.response_id,j.poll_count,j.result_published,j.log_path,
                j.location_slug,j.initialized_at,j.reset_pending,j.content_epoch,j.current_epoch FROM history j WHERE j.id=:id"""
                        + (" AND " + PUBLIC_GENERATION if public_only else "")
                    ),
                    {"id": job_id},
                )
                .mappings()
                .first()
            )
        return self.clean(self._job(dict(row))) if row else None

    def detail(self, job_id: str, *, public_only: bool = False) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            job = (
                conn.execute(
                    text(HISTORY + "SELECT j.* FROM history j WHERE j.id=:id"),
                    {"id": job_id},
                )
                .mappings()
                .first()
            )
            if job is None:
                return None
            if public_only and (job["kind"] != "generate" or not job["public_log"]):
                return None
            generation = (
                conn.execute(
                    text(
                        "SELECT request_json,response_json FROM generation_requests WHERE job_id=:id"
                    ),
                    {"id": job_id},
                )
                .mappings()
                .first()
            )
            diagnostics = None
            if generation:
                request_body = generation["request_json"] or {}
                response_body = generation["response_json"] or {}
                usage = response_body.get("usage") or {}
                reason = (response_body.get("incomplete_details") or {}).get("reason")
                diagnostics = {
                    "max_output_tokens": request_body.get("max_output_tokens"),
                    "input_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    "reasoning_tokens": (usage.get("output_tokens_details") or {}).get(
                        "reasoning_tokens"
                    ),
                    "incomplete_reason": reason,
                    "next_step": "Лимит ответа исчерпан. Генерация завершена. Нужен новый запрос с изменённым промптом или лимитом; проверка того же ответа его не продолжит."
                    if reason == "max_output_tokens"
                    else "",
                }
            calls = (
                conn.execute(
                    text(
                        "SELECT id,job_id,model,operation,status,input_tokens,output_tokens,detail,created_at FROM ai_usage WHERE job_id=:id ORDER BY created_at,id"
                    ),
                    {"id": job_id},
                )
                .mappings()
                .all()
            )
            rows = (
                conn.execute(
                    text("SELECT * FROM audit WHERE target=:id ORDER BY created_at,id"),
                    {"id": job_id},
                )
                .mappings()
                .all()
            )
            review = None
            events = []
            for row in rows:
                try:
                    payload = json.loads(row["detail"])
                except ValueError:
                    payload = {"event": row["action"], "data": row["detail"]}
                if row["action"] == "generation_run":
                    payload = {"event": "generation_run", "data": payload}
                event = {"time": row["created_at"], "payload": payload}
                if isinstance(payload, dict) and payload.get("event") == "response":
                    body = payload["data"].get("body", "")
                    try:
                        body = json.dumps(json.loads(body), ensure_ascii=False, indent=2)
                    except ValueError:
                        pass
                    event["response_body"] = body
                    event["response_meta"] = {
                        k: v for k, v in payload["data"].items() if k != "body"
                    }
                events.append(event)
        # Round-trip UUID/datetime/Decimal values to a portable downloadable JSON document.
        return json.loads(
            json.dumps(
                self.clean(
                    {
                        "job": self._job(dict(job)),
                        "calls": [dict(c) for c in calls],
                        "diagnostics": diagnostics,
                        "events": events,
                        "review": dict(review) if review else None,
                        "changes": job["changes"],
                        "trace_available": any(r["action"] == "ai_trace" for r in rows),
                    }
                ),
                ensure_ascii=False,
                default=str,
            )
        )
