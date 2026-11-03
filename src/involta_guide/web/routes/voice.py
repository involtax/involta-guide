from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.requests import Request as StarletteRequest

from involta_guide.domain.entities import NotFound
from involta_guide.infrastructure.voice.client import VoiceClient, VoiceUnavailable
from involta_guide.web.http import admin_token, check_origin, client_ip, context
from involta_voice.contracts import LocationContext, RestaurantContext, SubmissionMetadata


def visitor_identity(request: Request, visitor: str) -> str:
    return context(request).security.fingerprint("voice-visitor:" + visitor)


def voice_client(request: Request) -> VoiceClient:
    # A test transport can be injected without changing production authorization.
    return getattr(request.app.state, "voice_client", None) or VoiceClient(
        context(request).settings
    )


def owner(request: Request) -> str:
    visitor = request.cookies.get("guide_visitor", "")
    try:
        UUID(visitor)
    except ValueError:
        raise HTTPException(404, "Запись не найдена") from None
    return visitor_identity(request, visitor)


def editorial_status(request: Request, reference: str, visitor: str) -> dict[str, Any] | None:
    ctx = context(request)
    try:
        review = ctx.queries.get("reviews", reference)
    except NotFound:
        return None
    if review["visitor"] != visitor:
        raise HTTPException(404, "Запись не найдена")
    text = ctx.queries.get("review_texts", reference)
    location = ctx.queries.get("locations", review["location_id"])
    state = "fraud" if review["fraud"] else review["moderation"]
    versions = ctx.queries.rows("version_reviews", {"review_id": reference})
    if versions and review["moderation"] == "approved" and not review["fraud"]:
        from involta_guide.infrastructure.voice.read_model import VoiceReadModel

        linked = {v["version_id"] for v in versions}
        if location and location["published_version"] in linked:
            state = "published"
        elif location and VoiceReadModel(ctx.engine).page_candidate(str(location["id"])) in linked:
            state = "draft"
    if text and text["public"] and review["moderation"] == "approved" and not review["fraud"]:
        state = "published"
    return {
        "reference": reference,
        "transcript": text["original_text"] if text else review["transcript"],
        "moderation": review["moderation"],
        "editorial_state": state,
        "state": "result_ready",
    }


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
        response = JSONResponse({"ready": True}, headers={"Cache-Control": "private,no-store"})
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
            return JSONResponse(
                {"error": {"code": "unavailable", "message": "Приём новых записей Voice отключён"}},
                status_code=503,
            )
        if ctx.security.principal(admin_token(request)) or ctx.security.principal(
            request.cookies.get("guide_alex"), alex=True
        ):
            raise HTTPException(409, "Команды Алекса отправляются через редакторский виджет")
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > ctx.settings.max_audio_bytes + 100_000:
                raise HTTPException(413, "Запись больше 10 МБ")
            chunks.append(chunk)

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"".join(chunks), "more_body": False}

        form = await StarletteRequest(request.scope, receive).form(
            max_files=1, max_fields=8, max_part_size=ctx.settings.max_audio_bytes
        )
        try:
            audio = form.get("audio")
            if not isinstance(audio, UploadFile):
                raise HTTPException(400, "Нужна аудиозапись")
            public = ctx.queries.public(str(form.get("location") or ""))
            if public is None:
                raise HTTPException(404, "Локация не найдена")
            location = public["location"]
            selected = ctx.queries.location(location["id"])
            visitor = request.cookies.get("guide_visitor", "")
            try:
                UUID(visitor)
            except ValueError:
                visitor = str(uuid4())
            metadata = SubmissionMetadata(
                location=LocationContext(
                    id=location["id"],
                    slug=location["slug"],
                    name=location["name"],
                    revision=location["draft_version"],
                    published_revision=location["published_version"],
                    restaurants=[
                        RestaurantContext(
                            id=r["id"],
                            name=r["name"],
                            phone=r.get("phone", ""),
                            description=r.get("description", ""),
                            openingHours=r.get("openingHours"),
                        )
                        for r in selected["available"]
                    ][:200],
                ),
                visitor=visitor_identity(request, visitor),
                idempotency_key=UUID(str(form.get("idempotency_key") or "")),
                ip_fingerprint=ctx.security.fingerprint(client_ip(request)),
                user_agent=request.headers.get("user-agent", "")[:500],
                language=str(form.get("language") or "")[:40],
                timezone=str(form.get("timezone") or "")[:100],
                correlation_id=uuid4(),
            )
            client = voice_client(request)
            response = await run_in_threadpool(
                client.request,
                "POST",
                "/submissions",
                data={"metadata": metadata.model_dump_json()},
                files={"audio": ("recording", await audio.read(), audio.content_type or "")},
            )
            result = JSONResponse(
                response.json(),
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
        except VoiceUnavailable:
            return JSONResponse(
                {
                    "error": {
                        "code": "unavailable",
                        "message": "Сервис временно недоступен. Повторите отправку этой записи.",
                    }
                },
                status_code=503,
            )
        finally:
            await form.close()

    @router.get(base + "/submissions/mine")
    def mine(request: Request, page: int = 1, location: str = "") -> Response:
        if not request.cookies.get("guide_visitor"):
            return JSONResponse(
                {"rows": [], "page": page}, headers={"Cache-Control": "private,no-store"}
            )
        visitor = owner(request)
        try:
            data = voice_client(request).json(
                "GET", "/submissions", params={"visitor": visitor, "page": max(1, page)}
            )
            for item in data["rows"]:
                imported = editorial_status(request, item["reference"], visitor)
                if imported:
                    item.update({k: v for k, v in imported.items() if k != "state"})
        except VoiceUnavailable:
            rows = context(request).queries.rows("reviews", {"visitor": visitor}, limit=30)
            data = {
                "rows": [editorial_status(request, r["id"], visitor) for r in rows],
                "page": page,
                "service_available": False,
            }
        if location:
            public = context(request).queries.public(location)
            if public:
                data["rows"] = [
                    r
                    for r in data["rows"]
                    if str(r.get("location", {}).get("id")) == public["location"]["id"]
                ]
                legacy = context(request).queries.visitor(
                    request.cookies["guide_visitor"], public["location"]["id"]
                )
                for item in legacy:
                    source = context(request).queries.get("reviews", item["id"])
                    data["rows"].append(
                        {
                            "reference": "legacy:" + item["id"],
                            "transcript": source["transcript"],
                            "moderation": item["moderation"],
                            "editorial_state": "published"
                            if item["published"]
                            else "fraud"
                            if item["fraud"]
                            else item["moderation"],
                            "state": item["status"],
                            "created_at": str(item["created_at"]),
                            "legacy": True,
                        }
                    )
        return JSONResponse(data, headers={"Cache-Control": "private,no-store"})

    @router.get(base + "/submissions/{reference}")
    def status(request: Request, reference: UUID) -> Response:
        visitor = owner(request)
        imported = editorial_status(request, str(reference), visitor)
        try:
            remote = voice_client(request).request(
                "GET", "/submissions/" + str(reference), params={"visitor": visitor}
            )
            if remote.status_code == 404:
                raise HTTPException(404, "Запись не найдена")
            if not remote.is_success:
                raise VoiceUnavailable("Статус временно недоступен")
            data = remote.json()
        except VoiceUnavailable:
            if imported is None:
                raise HTTPException(503, "Статус временно недоступен") from None
            data = imported
        if imported:
            data.update({k: v for k, v in imported.items() if k != "state"})
        return JSONResponse(data, headers={"Cache-Control": "private,no-store"})

    return router
