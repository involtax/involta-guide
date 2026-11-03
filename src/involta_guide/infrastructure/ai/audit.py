"""Durable per-job AI traces in the existing PostgreSQL audit journal."""

import json
import re
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from pydantic import SecretStr
from sqlalchemy import Engine, text

from involta_guide.config import Settings

HISTORY = """WITH numbered AS (
 SELECT j.*,l.name location_name,r.location_slug,r.number ordinal,r.path log_path,
 g.state generation_state,g.response_id,g.poll_count,g.last_error generation_error,
 (g.response_json IS NOT NULL) has_saved_response,
 EXISTS(SELECT 1 FROM audit published WHERE published.action='ai_result' AND published.target=j.id::text
   AND published.detail::jsonb->'data'->>'version_id'=l.published_version::text) result_published,
 (SELECT a.detail::jsonb FROM audit a WHERE a.action='generation_run' AND a.target=j.id::text ORDER BY a.created_at LIMIT 1) generation_config,
 (SELECT u.model FROM ai_usage u WHERE u.job_id=j.id ORDER BY u.created_at,u.id LIMIT 1) actual_model
 FROM jobs j JOIN request_logs r ON r.job_id=j.id JOIN locations l ON l.slug=r.location_slug
 LEFT JOIN generation_requests g ON g.job_id=j.id
), history AS (
 SELECT *,ordinal run_number,
 coalesce(generation_config->>'model',actual_model,'') model_name FROM numbered
) """
PUBLIC_GENERATION = """j.kind='generate' AND j.review_id IS NULL AND
 (j.generation_config->>'public_log'='true' OR EXISTS(SELECT 1 FROM audit a WHERE a.action='temporary_public_fill' AND a.target=j.id::text))"""


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
                    else "Результат сохранён в истории"
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
        job["can_reprocess"] = (
            job["kind"] == "generate"
            and job["status"] in ("done", "failed", "cancelled")
            and state == "completed"
            and bool(job.get("has_saved_response"))
        )
        job["can_resume"] = (
            job["status"] == "failed"
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
                j.location_slug FROM history j WHERE j.id=:id"""
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
            if public_only and (
                job["kind"] != "generate"
                or job["review_id"] is not None
                or not (
                    (job["generation_config"] or {}).get("public_log")
                    or conn.execute(
                        text(
                            "SELECT 1 FROM audit WHERE target=:id AND action='temporary_public_fill' LIMIT 1"
                        ),
                        {"id": job_id},
                    ).first()
                )
            ):
                return None
            calls = (
                conn.execute(
                    text("SELECT * FROM ai_usage WHERE job_id=:id ORDER BY created_at,id"),
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
            if job["review_id"]:
                review = (
                    conn.execute(
                        text("SELECT * FROM reviews WHERE id=:id"), {"id": job["review_id"]}
                    )
                    .mappings()
                    .first()
                )
            events = []
            version_ids: set[str] = set()
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
                if isinstance(payload, dict) and payload.get("event") == "job_completed":
                    version_ids.update(v for v in payload.get("data", {}).get("versions", []) if v)
                if isinstance(payload, dict) and payload.get("event") == "result_saved":
                    version_ids.add(payload["data"]["version_id"])
            versions = [
                dict(row)
                for version_id in version_ids
                for row in conn.execute(
                    text("SELECT * FROM versions WHERE id=:id"), {"id": version_id}
                ).mappings()
            ]
        # Round-trip UUID/datetime/Decimal values to a portable downloadable JSON document.
        return json.loads(
            json.dumps(
                self.clean(
                    {
                        "job": self._job(dict(job)),
                        "calls": [dict(c) for c in calls],
                        "events": events,
                        "review": dict(review) if review else None,
                        "versions": versions,
                        "trace_available": any(r["action"] == "ai_trace" for r in rows),
                    }
                ),
                ensure_ascii=False,
                default=str,
            )
        )
