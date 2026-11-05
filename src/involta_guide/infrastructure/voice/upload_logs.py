"""A read-only view of existing upload audit events, grouped by HTTP request ID."""

import json
import math
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import Engine, text

from involta_guide.config import Settings
from involta_guide.infrastructure.voice.upload_debug import UploadDebug

STAGES = {
    "received": "Guide получил заголовки запроса",
    "body_received": "Guide полностью получил аудио",
    "body_progress": "Guide получил часть аудио",
    "forwarding": "Guide подготовил передачу аудио в Voice (с проверкой контекста)",
    "upstream_error": "Обращение к Voice завершилось ошибкой",
    "upstream_response": "Voice вернул ответ",
    "upstream": "Voice вернул ответ не в формате JSON",
    "response": "Guide завершил HTTP-ответ",
    "interrupted": "Запрос к Guide прерван",
    "browser": "Guide сохранил диагностику браузера",
}

# Source events stay in audit; status and URLs are derived, never stored separately.
REQUESTS = """
WITH grouped AS (
 SELECT target AS request_id, min(created_at) AS created_at, max(created_at) AS updated_at,
 COALESCE(min(location_id) FILTER (WHERE detail::jsonb->>'stage'<>'browser'),
          min(location_id)) AS location_id,
 (array_agg((detail::jsonb->>'status')::int ORDER BY created_at DESC,id DESC)
  FILTER (WHERE detail::jsonb->>'stage' IN ('response','interrupted')))[1] AS server_status,
 max(jsonb_path_query_first(detail::jsonb,
  '$.report.events[*] ? (@.status.type() == "number" && @.status >= 400 && @.status <= 599).status')::text::int)
  AS browser_status,
 bool_or(jsonb_path_exists(detail::jsonb, '$.report.events[*].exception')) AS browser_error,
 bool_or(detail::jsonb->>'stage'='interrupted') AS interrupted
 FROM audit WHERE action='voice_upload_http'
 AND COALESCE(detail::jsonb->>'path','') NOT LIKE '%/identity'
 GROUP BY target
), requests AS (
 SELECT g.*,l.slug AS location_slug,COALESCE(l.name,'Без локации') AS location_name,
 CASE WHEN server_status BETWEEN 200 AND 299 THEN 'done'
 WHEN server_status>=400 OR browser_status>=400 OR browser_error OR interrupted THEN 'failed'
 WHEN g.updated_at<now()-interval '90 seconds' THEN 'stalled' ELSE 'running' END AS status
 FROM grouped g LEFT JOIN locations l ON l.id=g.location_id
 WHERE (:q='' OR l.slug ILIKE :search OR l.name ILIKE :search OR request_id ILIKE :search)
)
"""


def label(state: str, server: int | None, browser: int | None) -> str:
    if state == "done":
        return "Принято" if server == 202 else "Запрос завершён"
    if server and server >= 400:
        return f"Guide: HTTP {server}"
    if browser and browser >= 400:
        return f"Браузер получил HTTP {browser}"
    return {"running": "В работе", "stalled": "Нет завершения"}.get(state, "Ошибка отправки")


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def timestamp(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(UTC) if result.tzinfo else None
    except (ValueError, OverflowError):
        return None


def technical_body(body: Any, status: Any = None) -> str:
    """Keep error details, excluding concrete private response/metadata fields."""
    if not isinstance(body, str):
        return ""
    try:
        payload = json.loads(body)
    except ValueError:
        # Preserve proxy/provider errors, including HTML. A successful non-JSON
        # response could instead be a private page, so only admins see that body.
        code = number(status)
        if code is not None and 400 <= code <= 599:
            return body
        return "Ответ не в формате JSON. Полное тело доступно администратору."

    code = number(status)
    if code is not None and 200 <= code <= 299:
        # A successful status can contain a transcript, model comparisons or
        # future private fields. Publish only its stable technical envelope.
        fields = (
            "reference",
            "state",
            "created_at",
            "moderation",
            "editorial_state",
            "content_epoch",
        )
        payload = (
            {k: payload[k] for k in fields if k in payload} if isinstance(payload, dict) else {}
        )

    def redact(value: Any) -> Any:
        if isinstance(value, dict):
            private = {
                "visitor",
                "owner",
                "ip",
                "user_agent",
                "metadata",
                "transcript",
                "text",
                "original_text",
                "editorial_text",
                "audio",
                "audio_url",
                "idempotency_key",
                "input",
                "ctx",
                "entries",
                "suggestions",
                "repeat_signals",
            }
            return {k: redact(v) for k, v in value.items() if k.lower() not in private}
        if isinstance(value, list):
            return [redact(v) for v in value]
        return value

    return json.dumps(redact(payload), ensure_ascii=False, indent=2)


def public_event(event: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "stage",
        "time",
        "path",
        "method",
        "content_type",
        "content_length",
        "status",
        "elapsed_ms",
        "received_bytes",
        "request_sha256",
        "response_headers",
        "message",
        "exception_type",
    )
    result = {k: event[k] for k in fields if k in event}
    if isinstance(result.get("response_headers"), dict):
        result["response_headers"] = {
            k: v
            for k, v in result["response_headers"].items()
            if k.lower() in ("content-type", "content-length", "x-voice-request-id")
        }
    if "response_body" in event:
        result["response_body"] = technical_body(event["response_body"], event.get("status"))
    report = event.get("report")
    if isinstance(report, dict):
        recording_field = "recording" if "recording" in report else "draft"
        draft = report.get(recording_field)
        visible: dict[str, Any] = {
            "version": report.get("version"),
            recording_field: {
                k: draft[k]
                for k in (
                    "bytes",
                    "mime",
                    "durationMs",
                    "content_epoch",
                    "local_copy_saved",
                    "blocked",
                )
                if isinstance(draft, dict) and k in draft
            },
            "events": [],
        }
        for item in report.get("events", []) if isinstance(report.get("events"), list) else []:
            if not isinstance(item, dict):
                continue
            safe = {
                k: item[k]
                for k in (
                    "method",
                    "started",
                    "status",
                    "status_text",
                    "elapsed_ms",
                    "response_headers",
                )
                if k in item
            }
            if isinstance(item.get("url"), str):
                try:
                    safe["path"] = urlsplit(item["url"]).path
                except ValueError:
                    pass
            if "response_body" in item:
                safe["response_body"] = technical_body(item["response_body"], item.get("status"))
            if isinstance(safe.get("response_headers"), dict):
                safe["response_headers"] = {
                    k: v
                    for k, v in safe["response_headers"].items()
                    if k.lower() in ("content-type", "content-length", "x-voice-request-id")
                }
            if isinstance(item.get("exception"), dict):
                safe["exception"] = {
                    k: item["exception"][k] for k in ("name", "message") if k in item["exception"]
                }
            visible["events"].append(safe)
        timeline = report.get("timeline")
        if isinstance(timeline, list):
            visible["timeline"] = [
                {
                    k: item[k]
                    for k in (
                        "name",
                        "ms",
                        "processed_ms",
                        "duration_ms",
                        "reason",
                        "bytes",
                        "mime",
                    )
                    if k in item
                }
                for item in timeline
                if isinstance(item, dict)
            ]
        result["report"] = visible
    return result


class UploadLogs:
    def __init__(self, engine: Engine, settings: Settings) -> None:
        self.engine, self.settings = engine, settings

    @staticmethod
    def decorate(row: dict[str, Any]) -> dict[str, Any]:
        row["stage_label"] = label(row["status"], row["server_status"], row["browser_status"])
        row["log_path"] = (
            f"/logs/{row['location_slug'] or 'unassigned'}/uploads/{row['request_id']}"
        )
        for key in ("created_at", "updated_at"):
            row[key] = row[key].isoformat()
        row.pop("location_id", None)
        return row

    def listing(self, page: int = 1, q: str = "", status: str = "") -> dict[str, Any]:
        params: dict[str, Any] = {"q": q.strip(), "search": f"%{q.strip()}%", "status": status}
        where = " WHERE (:status='' OR status=:status)"
        with self.engine.connect() as conn:
            total = conn.execute(
                text(REQUESTS + " SELECT count(*) FROM requests" + where), params
            ).scalar_one()
            pages = max(1, (total + 19) // 20)
            page = min(max(1, page), pages)
            rows = conn.execute(
                text(
                    REQUESTS
                    + " SELECT * FROM requests"
                    + where
                    + " ORDER BY created_at DESC,request_id LIMIT 20 OFFSET :offset"
                ),
                {**params, "offset": (page - 1) * 20},
            ).mappings()
            return {
                "rows": [self.decorate(dict(r)) for r in rows],
                "total": total,
                "page": page,
                "pages": pages,
            }

    def detail(self, request_id: str, *, admin: bool = False) -> dict[str, Any] | None:
        request_id = str(UUID(request_id))
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    text(REQUESTS + " SELECT * FROM requests WHERE request_id=:id"),
                    {"q": "", "search": "", "id": request_id},
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        request = self.decorate(dict(row))
        events = UploadDebug(self.engine, self.settings).read(request_id, "", admin=True)
        events = UploadDebug(self.engine, self.settings).clean(events)
        if not admin:
            events = [public_event(e) for e in events]
        timeline, bodies = [], []
        facts: dict[str, Any] = {}
        for event in events:
            at = timestamp(event.get("time"))
            stage = event.get("stage", "")
            timeline.append(
                {
                    "time": at.isoformat() if at else "",
                    "source": "Guide",
                    "label": STAGES.get(stage, stage),
                    "status": event.get("status"),
                    "bytes": event.get("received_bytes"),
                }
            )
            if event.get("content_length"):
                facts["declared_bytes"] = number(event["content_length"])
            if event.get("received_bytes") is not None:
                facts["received_bytes"] = number(event["received_bytes"])
            if event.get("response_body"):
                source = (
                    "Voice → Guide"
                    if stage in ("upstream", "upstream_response")
                    else "Guide → браузер"
                )
                bodies.append({"source": source, "body": event["response_body"]})
            report = event.get("report")
            if not isinstance(report, dict):
                continue
            draft = report.get("recording", report.get("draft"))
            if isinstance(draft, dict):
                facts.update(
                    {
                        "bytes": number(draft.get("bytes")),
                        "duration_ms": number(draft.get("durationMs")),
                        "mime": draft.get("mime"),
                        "local_copy_saved": draft.get("local_copy_saved") is True,
                    }
                )
            browser_events = report.get("events")
            if not isinstance(browser_events, list):
                continue
            for item in browser_events:
                if not isinstance(item, dict):
                    continue
                started, elapsed = timestamp(item.get("started")), number(item.get("elapsed_ms"))
                timeline.append(
                    {
                        "time": started.isoformat() if started else "",
                        "source": "Браузер",
                        "label": "Отправка запроса началась",
                        "status": None,
                    }
                )
                if elapsed is not None:
                    facts["elapsed_ms"] = elapsed
                if started and elapsed is not None and elapsed < 86_400_000:
                    try:
                        finished_time = (started + timedelta(milliseconds=elapsed)).isoformat()
                    except OverflowError:
                        finished_time = ""
                    timeline.append(
                        {
                            "time": finished_time,
                            "source": "Браузер",
                            "label": "Завершение запроса",
                            "status": item.get("status"),
                            "error": item.get("exception", {}).get("message")
                            if isinstance(item.get("exception"), dict)
                            else None,
                        }
                    )
                if item.get("response_body"):
                    bodies.append(
                        {"source": "Ответ, полученный браузером", "body": item["response_body"]}
                    )
        timeline.sort(key=lambda e: e["time"])
        stages = {e.get("stage") for e in events}
        facts["body_complete"] = "body_received" in stages or request["status"] == "done"
        if request["status"] == "done":
            explanation = "Guide завершил запрос успешно."
        elif "received" in stages and "body_received" not in stages and "response" not in stages:
            explanation = "Guide зарегистрировал запрос. Завершение приёма аудио и ответ Guide в журнале отсутствуют."
        elif "body_received" in stages and not stages & {"response", "interrupted"}:
            explanation = (
                "Аудио полностью получено Guide. Завершение HTTP-ответа не зарегистрировано."
            )
        else:
            explanation = "Подробности ответа и этапы отправки приведены ниже."
        if request["browser_status"] and request["status"] == "done":
            explanation += (
                " Браузер сообщил об ошибке; серверный журнал подтверждает успешный ответ."
            )
        return {
            "request": request,
            "facts": facts,
            "timeline": timeline,
            "events": events,
            "bodies": bodies,
            "explanation": explanation,
        }
