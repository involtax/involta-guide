import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.datastructures import UploadFile
from starlette.requests import Request as StarletteRequest

from .config import Settings
from .contracts import LocationContext, SubmissionMetadata, SubmissionStatus
from .db import database, encoded, execute, row, snapshot
from .errors import VoiceError
from .service import VoiceService
from .worker import Worker

MODELS = {
    "transcription": ["large-v3-russian", "large-v3"],
    "analysis": ["gpt-4.1-mini", "gpt-4.1", "gpt-6.1-sol"],
}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = database(settings)
    service = VoiceService(engine, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        engine.dispose()

    app = FastAPI(title="Involta Voice", version="1.0.0", lifespan=lifespan)
    app.state.service = service

    @app.exception_handler(VoiceError)
    async def voice_error(request: Request, exc: VoiceError) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": exc.code, "message": str(exc)}}, status_code=exc.status
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": "unavailable", "message": "Хранилище временно недоступно"}},
            status_code=503,
        )

    @app.exception_handler(ValueError)
    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": "invalid", "message": "Некорректные данные запроса"}},
            status_code=400,
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "code": "unauthorized" if exc.status_code == 401 else "invalid",
                    "message": str(exc.detail),
                }
            },
            status_code=exc.status_code,
        )

    bearer = HTTPBearer(auto_error=False)

    def authorized(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:
        expected = settings.service_secret.get_secret_value()
        if (
            credentials is None
            or credentials.scheme.lower() != "bearer"
            or not hmac.compare_digest(credentials.credentials, expected)
        ):
            raise HTTPException(401, "Invalid service credential")

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready")
    def ready() -> dict[str, str]:
        with engine.connect() as conn:
            execute(conn, "SELECT version FROM voice_schema_versions")
        return {"status": "ready"}

    @app.get("/widget.v1.js")
    def widget() -> FileResponse:
        return FileResponse(
            Path(__file__).parent / "static/widget.v1.js",
            media_type="application/javascript",
            headers={"Cache-Control": "public,max-age=300"},
        )

    dependencies = [Depends(authorized)]

    @app.post("/internal/v1/admin/archive", dependencies=dependencies)
    async def archive(request: Request) -> Any:
        from starlette.concurrency import run_in_threadpool

        from .archive import copy_archive

        chunks = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > settings.max_audio_bytes + 200_000:
                raise VoiceError("Запись слишком велика", "too_large", 413)
            chunks.append(chunk)

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"".join(chunks), "more_body": False}

        form = await StarletteRequest(request.scope, receive).form(
            max_files=1, max_fields=3, max_part_size=settings.max_audio_bytes
        )
        try:
            audio = form.get("audio")
            if not isinstance(audio, UploadFile):
                raise VoiceError("Нужна исходная аудиозапись")
            meta = SubmissionMetadata.model_validate_json(str(form.get("metadata") or ""))
            source = UUID(str(form.get("source_review") or ""))
            import json

            legacy = json.loads(str(form.get("legacy") or ""))
            if set(legacy) - {
                "sha256",
                "source_review",
                "created_at",
                "moderation",
                "fraud",
                "result",
                "original_text",
                "editorial_text",
            }:
                raise VoiceError("Неизвестное поле архива")
            return await run_in_threadpool(
                copy_archive,
                service,
                source,
                meta,
                await audio.read(),
                (audio.content_type or "").split(";")[0],
                legacy,
            )
        finally:
            await form.close()

    def audit_admin(request: Request, action: str, before: Any, after: Any) -> None:
        session = request.headers.get("x-guide-session")
        session_id = str(UUID(session)) if session else None
        with engine.begin() as conn:
            execute(
                conn,
                "INSERT INTO admin_events(id,action,session_id,before_data,after_data) VALUES(:id,:action,:session,CAST(:before AS jsonb),CAST(:after AS jsonb))",
                id=str(uuid4()),
                action=action,
                session=session_id,
                before=encoded(before),
                after=encoded(after),
            )

    @app.post(
        "/internal/v1/submissions",
        dependencies=dependencies,
        status_code=202,
        response_model=SubmissionStatus,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "multipart/form-data": {
                        "schema": {
                            "type": "object",
                            "required": ["audio", "metadata"],
                            "properties": {
                                "audio": {"type": "string", "format": "binary"},
                                "metadata": {
                                    "type": "string",
                                    "description": "SubmissionMetadata v1 JSON, trusted Guide identity/context",
                                },
                            },
                        }
                    }
                },
            }
        },
    )
    async def submit(request: Request) -> Any:
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > settings.max_audio_bytes + 100_000:
                raise VoiceError("Запись больше 10 МБ", "too_large", 413)
            chunks.append(chunk)

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"".join(chunks), "more_body": False}

        bounded = StarletteRequest(request.scope, receive)
        form = await bounded.form(max_files=1, max_fields=2, max_part_size=settings.max_audio_bytes)
        try:
            audio = form.get("audio")
            if not isinstance(audio, UploadFile):
                raise VoiceError("Аудиозапись обязательна")
            metadata = SubmissionMetadata.model_validate_json(str(form.get("metadata") or ""))
            from starlette.concurrency import run_in_threadpool

            return await run_in_threadpool(
                service.submit,
                metadata,
                await audio.read(),
                (audio.content_type or "").split(";")[0],
            )
        except ValueError as exc:
            if isinstance(exc, VoiceError):
                raise
            raise VoiceError("Некорректные метаданные") from exc
        finally:
            await form.close()

    @app.get("/internal/v1/submissions", dependencies=dependencies)
    def mine(visitor: str, page: int = 1) -> Any:
        return service.listing(visitor, page)

    @app.get(
        "/internal/v1/submissions/{reference}",
        dependencies=dependencies,
        response_model=SubmissionStatus,
    )
    def status(reference: UUID, visitor: str) -> Any:
        return service.status(str(reference), visitor)

    @app.get("/internal/v1/events", dependencies=dependencies)
    def events(after: int = 0, limit: int = 100) -> Any:
        with engine.connect() as conn:
            values = conn.execute(
                text(
                    "SELECT sequence,payload FROM outbox WHERE sequence>:after ORDER BY sequence LIMIT :limit"
                ),
                {"after": max(0, after), "limit": max(1, min(100, limit))},
            ).mappings()
            return {"events": [{"sequence": r["sequence"], **r["payload"]} for r in values]}

    @app.post("/internal/v1/events/ack", dependencies=dependencies)
    def ack(body: dict[str, Any]) -> dict[str, bool]:
        sequence = max(0, int(body.get("sequence", 0)))
        with engine.begin() as conn:
            execute(
                conn,
                """INSERT INTO delivery_ack(consumer,sequence) VALUES('guide',:n)
                ON CONFLICT(consumer) DO UPDATE SET sequence=greatest(delivery_ack.sequence,:n)""",
                n=sequence,
            )
        return {"acknowledged": True}

    @app.get("/internal/v1/admin/submissions", dependencies=dependencies)
    def admin_listing(page: int = 1) -> Any:
        return service.listing(page=page)

    @app.get("/internal/v1/admin/submissions/{reference}", dependencies=dependencies)
    def admin_detail(reference: UUID) -> Any:
        return service.status(str(reference), admin=True)

    @app.get("/internal/v1/admin/submissions/{reference}/audio", dependencies=dependencies)
    def audio(reference: UUID) -> FileResponse:
        with engine.connect() as conn:
            submission = row(
                conn, "SELECT audio,mime FROM submissions WHERE reference=:id", id=str(reference)
            )
        if not submission:
            raise VoiceError("Запись не найдена", "not_found", 404)
        return FileResponse(
            service.storage.path(submission["audio"]),
            media_type=submission["mime"],
            headers={"Cache-Control": "private,no-store"},
        )

    @app.get("/internal/v1/admin/submissions/{reference}/runs/{number}", dependencies=dependencies)
    def run_detail(reference: UUID, number: int) -> Any:
        with engine.connect() as conn:
            run = row(
                conn,
                "SELECT * FROM runs WHERE submission=:id AND number=:n",
                id=str(reference),
                n=number,
            )
            if not run:
                raise VoiceError("Обработка не найдена", "not_found", 404)
            calls = [
                dict(c)
                for c in conn.execute(
                    text(
                        "SELECT * FROM provider_calls WHERE submission=:id AND run=:n ORDER BY created_at,id"
                    ),
                    {"id": str(reference), "n": number},
                ).mappings()
            ]
        return {**run, "calls": calls}

    @app.post(
        "/internal/v1/admin/submissions/{reference}/runs",
        dependencies=dependencies,
        status_code=202,
    )
    def new_run(request: Request, reference: UUID, body: dict[str, Any]) -> Any:
        key = str(UUID(str(body.get("idempotency_key") or "")))
        frozen_context = (
            LocationContext.model_validate(body["context"]).model_dump(mode="json")
            if body.get("context")
            else None
        )
        before = service.status(str(reference))
        after = service.new_run(str(reference), key, frozen_context)
        audit_admin(request, "new-run", before, after)
        return after

    @app.post(
        "/internal/v1/admin/submissions/{reference}/runs/{number}/revalidate",
        dependencies=dependencies,
    )
    def revalidate(request: Request, reference: UUID, number: int) -> Any:
        worker = Worker(engine, settings)
        try:
            worker.revalidate(str(reference), number)
        except ValueError as exc:
            raise VoiceError("Сохранённый ответ не прошёл проверку", "invalid", 400) from exc
        finally:
            worker.provider.close()
        after = service.status(str(reference), admin=True)
        audit_admin(request, "revalidate", {"reference": str(reference), "run": number}, after)
        return after

    @app.get("/internal/v1/admin/settings", dependencies=dependencies)
    def get_settings() -> Any:
        with engine.connect() as conn:
            return {**snapshot(conn, settings), "models": MODELS, "prices_checked": "2026-11-03"}

    @app.put("/internal/v1/admin/settings", dependencies=dependencies)
    def put_settings(request: Request, body: dict[str, Any]) -> Any:
        if set(body) - {"transcription_model", "analysis_model", "prompt", "prices"}:
            raise VoiceError("Неизвестная настройка")
        for key, kind in (("transcription_model", "transcription"), ("analysis_model", "analysis")):
            if key in body and body[key] not in MODELS[kind]:
                raise VoiceError("Выберите модель из каталога")
        if "prompt" in body and (
            not isinstance(body["prompt"], str) or not 50 <= len(body["prompt"]) <= 30000
        ):
            raise VoiceError("Промпт должен содержать 50–30000 символов")
        if "prices" in body:
            prices = body["prices"]
            if not isinstance(prices, dict) or any(
                not isinstance(rates, dict)
                or set(rates) - {"input", "output"}
                or any(type(v) not in (int, float) or not 0 <= v <= 10000 for v in rates.values())
                for rates in prices.values()
            ):
                raise VoiceError("Некорректный каталог стоимости")
        before = get_settings()
        with engine.begin() as conn:
            for key, value in body.items():
                execute(
                    conn,
                    "INSERT INTO voice_settings(key,value) VALUES(:key,CAST(:value AS jsonb)) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    key=key,
                    value=encoded(value),
                )
        after = get_settings()
        audit_admin(request, "settings", before, after)
        return after

    return app


def worker_health() -> None:
    settings = Settings()
    age = datetime.now(UTC) - datetime.fromisoformat(settings.heartbeat_file.read_text())
    if age > timedelta(seconds=settings.lease_seconds):
        raise SystemExit(1)
    engine = database(settings)
    try:
        with engine.connect() as conn:
            execute(conn, "SELECT version FROM voice_schema_versions")
    finally:
        engine.dispose()
