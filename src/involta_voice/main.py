import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.requests import Request as StarletteRequest

from .config import Settings
from .contracts import (
    LocationPurged,
    LocationReset,
    SubmissionMetadata,
    SubmissionStatus,
)
from .db import database, execute, row
from .errors import VoiceError
from .service import VoiceService


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
            {"error": {"code": exc.code, "message": str(exc), **exc.details}},
            status_code=exc.status,
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
            if not row(conn, "SELECT 1 FROM voice_schema_versions WHERE version=9") or row(
                conn, "SELECT 1 FROM purge_files WHERE content_epoch=1 LIMIT 1"
            ):
                raise HTTPException(
                    503, "Переход Voice не завершён: проверьте миграции и очистку файлов"
                )
        return {"status": "ready"}

    app.mount("/vad", StaticFiles(directory=Path(__file__).parent / "static/vad"), name="vad")

    @app.get("/widget.v1.js")
    def widget() -> FileResponse:
        return FileResponse(
            Path(__file__).parent / "static/widget.v1.js",
            media_type="application/javascript",
            headers={"Cache-Control": "public,max-age=300"},
        )

    dependencies = [Depends(authorized)]

    @app.post(
        "/internal/v1/locations/{location_id}/reset",
        dependencies=dependencies,
        response_model=LocationPurged,
    )
    def reset_location(location_id: int, body: LocationReset) -> Any:
        from .reset import purge

        return purge(service, location_id, body.content_epoch)

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
                                    "description": "SubmissionMetadata v2 JSON, trusted Guide identity/context",
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
    def audio(reference: UUID, request: Request, playback: bool = False) -> Response:
        with engine.connect() as conn:
            submission = row(
                conn, "SELECT audio,mime FROM submissions WHERE reference=:id", id=str(reference)
            )
        if not submission:
            raise VoiceError("Запись не найдена", "not_found", 404)
        if playback and submission["mime"] == "audio/webm":
            from .playback import playback_response, seekable_webm

            return playback_response(
                seekable_webm(service.storage.path(submission["audio"])),
                request.headers.get("range", ""),
                request.headers.get("if-range", ""),
            )
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
                        "SELECT id,submission,run,kind,request,response,error,input_tokens,output_tokens,created_at FROM provider_calls WHERE submission=:id AND run=:n ORDER BY created_at,id"
                    ),
                    {"id": str(reference), "n": number},
                ).mappings()
            ]
        return {**run, "calls": calls}

    @app.get("/internal/v1/admin/submissions/{reference}/asr", dependencies=dependencies)
    def asr_report(reference: UUID) -> Any:
        from .recognition import report

        return report(engine, str(reference))

    @app.post("/internal/v1/admin/recognitions", dependencies=dependencies)
    def recognitions(body: list[UUID]) -> Any:
        from .recognition import reports

        if len(body) > 30:
            raise VoiceError("Не более 30 записей за один запрос")
        return reports(engine, [str(reference) for reference in body])

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
