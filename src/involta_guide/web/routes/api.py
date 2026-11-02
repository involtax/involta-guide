from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.requests import Request as StarletteRequest

from involta_guide.web.http import check_csrf, check_origin, client_ip, context, require_admin


def routes(prefix: str) -> APIRouter:
    router = APIRouter()

    @router.post(prefix + "/api/reviews")
    async def upload(request: Request) -> Response:
        check_origin(request)
        ctx = context(request)
        limit = ctx.settings.max_audio_bytes + 1024 * 1024
        chunks = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > limit:
                raise HTTPException(413, "Запись слишком большая (до 10 МБ)")
            chunks.append(chunk)
        body = b"".join(chunks)

        async def receive() -> dict:
            return {"type": "http.request", "body": body, "more_body": False}

        bounded = StarletteRequest(request.scope, receive)
        form = await bounded.form(max_files=1, max_fields=10, max_part_size=limit)
        try:
            audio = form.get("audio")
            location_id = str(UUID(str(form.get("locationId") or form.get("cityId") or "")))
            if form.get("consent") != "yes" or not isinstance(audio, UploadFile):
                raise HTTPException(400, "Нужна запись и согласие")
            visitor = request.cookies.get("guide_visitor", "")
            try:
                UUID(visitor)
            except ValueError:
                visitor = str(uuid4())
            alex = await run_in_threadpool(
                ctx.security.principal, request.cookies.get("guide_alex"), alex=True
            )
            if alex:
                check_csrf(request, form, alex=True)
            mime = (audio.content_type or "").split(";")[0]
            data = await audio.read()
            key = await run_in_threadpool(
                ctx.reviews.submit,
                location_id,
                visitor,
                ctx.security.fingerprint(client_ip(request)),
                request.headers.get("user-agent", ""),
                data,
                mime,
                alex["id"] if alex else None,
            )
            response = JSONResponse({"id": key}, status_code=201)
            response.set_cookie(
                "guide_visitor",
                visitor,
                max_age=365 * 86400,
                path=prefix,
                httponly=True,
                secure=ctx.settings.cookie_secure,
                samesite="lax",
            )
            return response
        finally:
            await form.close()

    @router.get(prefix + "/api/my-reviews")
    def history(request: Request, location: str = "", city: str = "") -> Response:
        ctx = context(request)
        visitor = request.cookies.get("guide_visitor")
        if not visitor:
            return JSONResponse([])
        try:
            key = str(UUID(location or city))
            UUID(visitor)
        except ValueError:
            return JSONResponse([])
        rows = ctx.queries.visitor(visitor, key)
        for row in rows:
            row["created_at"] = row["created_at"].strftime("%Y-%m-%d %H:%M:%S")
        return JSONResponse(jsonable_encoder(rows), headers={"Cache-Control": "no-store"})

    @router.get(prefix + "/api/admin/audio/{review_id}")
    def admin_audio(request: Request, review_id: str) -> Response:
        require_admin(request)
        try:
            review_id = str(UUID(review_id))
        except ValueError:
            raise HTTPException(404, "Запись не найдена") from None
        data, mime = context(request).reviews.audio(review_id)
        return Response(
            data,
            media_type=mime,
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    @router.get(prefix + "/api/alex/audio/{review_id}")
    def alex_audio(request: Request, review_id: str) -> Response:
        ctx = context(request)
        if not ctx.security.principal(request.cookies.get("guide_alex"), alex=True):
            require_admin(request)
        try:
            review_id = str(UUID(review_id))
        except ValueError:
            raise HTTPException(404, "Запись не найдена") from None
        data, mime = ctx.reviews.audio(review_id, alex_only=True)
        return Response(
            data,
            media_type=mime,
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    @router.get(prefix + "/api/admin/locations-export")
    @router.get(prefix + "/admin/api/locations-export")
    def export(request: Request) -> Response:
        require_admin(request)
        return JSONResponse(
            jsonable_encoder(context(request).queries.rows("locations", limit=100000)),
            headers={"Cache-Control": "no-store"},
        )

    return router
