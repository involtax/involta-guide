from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from involta_guide.web.http import (
    context,
    require_admin,
)


def routes(prefix: str) -> APIRouter:
    router = APIRouter()

    @router.get(prefix + "/api/admin/audio/{review_id}")
    def admin_audio(request: Request, review_id: str) -> Response:
        require_admin(request)
        try:
            review_id = str(UUID(review_id))
        except ValueError:
            raise HTTPException(404, "Запись не найдена") from None
        ctx = context(request)
        ctx.queries.get("reviews", review_id)
        from involta_guide.web.routes.voice import voice_client

        remote = voice_client(request).request("GET", "/admin/submissions/" + review_id + "/audio")
        return Response(
            remote.content,
            status_code=remote.status_code,
            media_type=remote.headers.get("content-type", "application/octet-stream"),
            headers={"Cache-Control": "private,no-store"},
        )

    return router
