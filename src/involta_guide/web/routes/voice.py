from io import BytesIO
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text as sql_text
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.requests import Request as StarletteRequest

from involta_guide.infrastructure.voice.client import VoiceClient, VoiceUnavailable
from involta_guide.web.http import admin_token, check_origin, client_ip, context
from involta_voice.contracts import LocationContext, RestaurantContext, SubmissionMetadata


def voice_client(request: Request) -> VoiceClient:
    # A test transport can be injected without changing production authorization.
    return getattr(request.app.state, "voice_client", None) or VoiceClient(
        context(request).settings
    )


def current_location_context(request: Request, location: dict[str, Any]) -> LocationContext:
    ctx = context(request)
    selected = ctx.queries.location(location["id"])
    with ctx.engine.connect() as conn:
        names = list(
            conn.execute(
                sql_text("SELECT name FROM restaurants WHERE location_id=:id ORDER BY name,id"),
                {"id": location["id"]},
            ).scalars()
        )
    from involta_voice.variants import dictionary_names

    return LocationContext(
        id=location["id"],
        slug=location["slug"],
        name=location["name"],
        revision=str(location["revision"]) if location["revision"] is not None else None,
        content_epoch=location["content_epoch"],
        recognition_dictionary=dictionary_names([location["name"], *names]),
        restaurants=[
            RestaurantContext(
                id=r["id"],
                name=r["name"],
                phone=r.get("phone") or "",
                description=r.get("description") or "",
                openingHours=r.get("openingHours"),
            )
            for r in selected["available"]
        ][:200],
    )


def owner(request: Request) -> str:
    visitor = request.cookies.get("guide_visitor", "")
    try:
        UUID(visitor)
    except ValueError:
        raise HTTPException(404, "Запись не найдена") from None
    return visitor


def routes(prefix: str) -> APIRouter:
    router = APIRouter()
    base = prefix + "/voice/api/v1"

    @router.post(base + "/identity")
    def identity(request: Request) -> Response:
        check_origin(request)
        visitor = request.cookies.get("guide_visitor", "")
        try:
            UUID(visitor)
        except ValueError:
            visitor = str(uuid4())
        ctx = context(request)
        ctx.queries.health()
        public = ctx.queries.public(request.query_params.get("location", ""))
        if not public or public["location"]["is_hub"]:
            raise HTTPException(404, "Выберите обычную локацию")
        location = public["location"]
        if location["reset_pending"]:
            raise HTTPException(
                409,
                detail={
                    "code": "stale_epoch",
                    "message": "Локация сбрасывается. Запишите новый отзыв после завершения.",
                },
            )
        response = JSONResponse(
            {"ready": True, "content_epoch": location["content_epoch"]},
            headers={"Cache-Control": "private,no-store"},
        )
        response.set_cookie(
            "guide_visitor",
            visitor,
            max_age=365 * 86400,
            path=prefix,
            httponly=True,
            secure=context(request).settings.cookie_secure,
            samesite="lax",
        )
        return response

    @router.get(base + "/debug/{request_id}")
    def upload_debug(request: Request, request_id: UUID) -> Response:
        from involta_guide.infrastructure.voice.upload_debug import UploadDebug

        ctx = context(request)
        if not ctx.settings.voice_debug_enabled:
            raise HTTPException(404, "Диагностика отключена")
        cookie = request.cookies.get("guide_visitor", "")
        identity = cookie
        administrator = bool(ctx.security.principal(admin_token(request)))
        events = UploadDebug(ctx.engine, ctx.settings).read(
            str(request_id), identity, administrator
        )
        return JSONResponse(
            {"request_id": str(request_id), "events": events},
            headers={"Cache-Control": "private,no-store"},
        )

    @router.post(base + "/debug/{request_id}")
    async def save_upload_debug(request: Request, request_id: UUID) -> Response:
        from involta_guide.infrastructure.voice.upload_debug import UploadDebug

        check_origin(request)
        ctx = context(request)
        if not ctx.settings.voice_debug_enabled:
            raise HTTPException(404, "Диагностика отключена")
        visitor = owner(request)
        public = ctx.queries.public(request.query_params.get("location", ""))
        if (
            not public
            or str(public["location"]["content_epoch"]) != request.query_params.get("content_epoch")
            or public["location"]["reset_pending"]
            or public["location"]["is_hub"]
        ):
            raise HTTPException(
                409, detail={"code": "stale_epoch", "message": "Контекст записи устарел"}
            )
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 1024 * 1024:
                raise HTTPException(413, "Отчёт диагностики больше 1 МБ")
            chunks.append(chunk)
        import json

        report = json.loads(b"".join(chunks))
        if not isinstance(report, dict):
            raise HTTPException(400, "Нужен отчёт диагностики")
        # Browser observations are untrusted evidence, kept beside server events.
        private = (*request.cookies.values(), request.headers.get("x-csrf-token", ""))
        await run_in_threadpool(
            UploadDebug(ctx.engine, ctx.settings).append,
            str(request_id),
            {
                "stage": "browser",
                "_context": {
                    "slug": request.query_params.get("location", ""),
                    "content_epoch": request.query_params.get("content_epoch", "0"),
                },
                "owner": request.cookies["guide_visitor"],
                "visitor": visitor,
                "report": {
                    key: report[key]
                    for key in (
                        "version",
                        "browser",
                        "role",
                        "location",
                        "draft",
                        "recording",
                        "events",
                        "timeline",
                        "vad",
                    )
                    if key in report
                },
            },
            private,
        )
        return JSONResponse({"saved": True}, headers={"Cache-Control": "private,no-store"})

    @router.get(prefix + "/voice/widget.v1.js")
    def widget(request: Request) -> Response:
        # Asset is shipped in the Voice package; no availability dependency on the service.
        from pathlib import Path

        from fastapi.responses import FileResponse

        import involta_voice

        assert involta_voice.__file__ is not None
        return FileResponse(
            Path(involta_voice.__file__).parent / "static/widget.v1.js",
            media_type="application/javascript",
            headers={"Cache-Control": "public,max-age=300"},
        )

    @router.post(base + "/submissions")
    async def submit(request: Request) -> Response:
        check_origin(request)
        ctx = context(request)
        if not ctx.settings.voice_enabled:
            raise HTTPException(503, "Приём новых записей Voice отключён")
        ctx.queries.health()
        public = ctx.queries.public(request.query_params.get("location", ""))
        if not public:
            raise HTTPException(404, "Локация не найдена")
        location = public["location"]
        try:
            epoch = int(request.query_params.get("content_epoch", ""))
        except ValueError:
            epoch = 0
        request.state.voice_context = {"location_id": location["id"], "content_epoch": epoch}
        if location["is_hub"]:
            raise HTTPException(400, "Выберите подлокацию для отзыва")
        if location["reset_pending"] or epoch != location["content_epoch"]:
            raise HTTPException(
                409,
                detail={
                    "code": "stale_epoch",
                    "message": "Локация сброшена. Запишите новый отзыв.",
                },
            )
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > ctx.settings.max_audio_bytes + 100_000:
                raise HTTPException(413, "Запись больше 10 МБ")
            chunks.append(chunk)

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"".join(chunks), "more_body": False}

        if request.headers.get("content-type", "").lower().startswith("audio/"):
            form = FormData(
                [
                    *request.query_params.multi_items(),
                    ("_csrf", request.headers.get("x-csrf-token", "")),
                    (
                        "audio",
                        UploadFile(
                            BytesIO(b"".join(chunks)), filename="recording", headers=request.headers
                        ),
                    ),
                ]
            )
        else:
            form = await StarletteRequest(request.scope, receive).form(
                max_files=1, max_fields=8, max_part_size=ctx.settings.max_audio_bytes
            )
        try:
            audio = form.get("audio")
            if not isinstance(audio, UploadFile):
                raise HTTPException(400, "Нужна аудиозапись")
            visitor = owner(request)
            metadata = SubmissionMetadata(
                location=current_location_context(request, location),
                visitor=visitor,
                statistics_excluded=bool(
                    await run_in_threadpool(ctx.security.principal, admin_token(request))
                ),
                idempotency_key=UUID(str(form.get("idempotency_key") or "")),
                ip=client_ip(request),
                user_agent=request.headers.get("user-agent", ""),
                language=str(form.get("language") or "")[:40],
                timezone=str(form.get("timezone") or "")[:100],
                correlation_id=UUID(request.state.voice_request_id)
                if getattr(request.state, "voice_request_id", None)
                else uuid4(),
            )
            audio_content = await audio.read()
            # Audit outside the epoch guard: a reset waiting for FOR UPDATE must
            # not deadlock with a diagnostic insert acquiring its own FOR SHARE.
            await request.state.voice_upload_record({"stage": "forwarding"})
            with ctx.engine.begin() as guard:
                live = (
                    guard.execute(
                        sql_text(
                            "SELECT content_epoch,reset_pending FROM locations WHERE id=:id FOR SHARE"
                        ),
                        {"id": location["id"]},
                    )
                    .mappings()
                    .one()
                )
                if live["reset_pending"] or live["content_epoch"] != epoch:
                    raise HTTPException(
                        409,
                        detail={
                            "code": "stale_epoch",
                            "message": "Локация сброшена. Запишите новый отзыв.",
                        },
                    )
                client = voice_client(request)
                response = await run_in_threadpool(
                    client.request,
                    "POST",
                    "/submissions",
                    data={"metadata": metadata.model_dump_json()},
                    files={"audio": ("recording", audio_content, audio.content_type or "")},
                )
            await request.state.voice_upload_record(
                {"stage": "upstream_response", "status": response.status_code}
            )
            try:
                payload = response.json()
            except ValueError:
                from involta_guide.infrastructure.voice.upload_debug import UploadDebug

                if ctx.settings.voice_debug_enabled:
                    await run_in_threadpool(
                        UploadDebug(ctx.engine, ctx.settings).append,
                        str(metadata.correlation_id),
                        {
                            "stage": "upstream",
                            "_context": {"slug": location["slug"], "content_epoch": epoch},
                            "status": response.status_code,
                            "response_body": response.text,
                            "owner": visitor,
                        },
                    )
                raise VoiceUnavailable("Voice вернул ответ не в формате JSON") from None
            result = JSONResponse(
                payload,
                status_code=response.status_code,
                headers={"Cache-Control": "private,no-store"},
            )
            result.set_cookie(
                "guide_visitor",
                visitor,
                max_age=365 * 86400,
                path=prefix,
                httponly=True,
                secure=ctx.settings.cookie_secure,
                samesite="lax",
            )
            return result
        except VoiceUnavailable as exc:
            await request.state.voice_upload_record(
                {
                    "stage": "upstream_error",
                    "message": str(exc),
                    "exception_type": type(exc.__cause__).__name__
                    if exc.__cause__
                    else type(exc).__name__,
                    "exception": str(exc.__cause__ or ""),
                }
            )
            return JSONResponse(
                {
                    "error": {
                        "code": "unavailable",
                        "message": "Сервис временно недоступен. Запишите новый отзыв позже.",
                    }
                },
                status_code=503,
            )
        finally:
            await form.close()

    @router.get(base + "/reviews")
    def public_reviews(request: Request, location: str, pending: str = "") -> Response:
        from involta_guide.infrastructure.voice.read_model import VoiceReadModel

        ctx = context(request)
        public = ctx.queries.public(location)
        if not public:
            raise HTTPException(404, "Локация не найдена")
        city = public["location"]
        read = VoiceReadModel(ctx.engine)
        visitor = request.cookies.get("guide_visitor") or None
        rows = read.public_texts(city["id"], visitor=visitor)
        try:
            references = [str(UUID(value)) for value in pending.split(",")[:30] if value]
        except ValueError:
            raise HTTPException(422, "Некорректный идентификатор отзыва") from None
        states = read.publication_states(city["id"], references, visitor=visitor)
        rows = [item for item in rows if states.get(item["reference"], 1) == 1]
        for item in rows:
            item["log_url"] = ctx.settings.path(
                "/logs/reviews/" + city["slug"] + "/" + str(item["number"])
            )
        from fastapi.encoders import jsonable_encoder

        return JSONResponse(
            jsonable_encoder(
                {
                    "rows": rows,
                    "content_epoch": city["content_epoch"],
                    "publication_statuses": states,
                }
            ),
            headers={"Cache-Control": "no-store"},
        )

    return router
