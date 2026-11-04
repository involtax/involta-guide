from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import RedirectResponse, Response
from starlette.datastructures import FormData

from involta_guide.infrastructure.voice.client import VoiceUnavailable
from involta_guide.infrastructure.voice.read_model import VoiceReadModel
from involta_guide.web.http import check_csrf, context, form_data, require_admin
from involta_guide.web.routes.admin import render
from involta_guide.web.routes.voice import voice_client


def routes(prefix: str) -> APIRouter:
    router = APIRouter()
    base = prefix + "/admin/reviews"

    def show(request: Request, slug: str | None = None, page: int = 1) -> Response:
        require_admin(request)
        ctx = context(request)
        query = request.query_params.get("q", "")
        listing = VoiceReadModel(ctx.engine).listing(slug, query, page)
        references = [r["reference"] for r in listing["rows"] if r["reference"]]
        comparisons: dict[str, Any] = {}
        unavailable = ""
        if references:
            try:
                comparisons = voice_client(request).json(
                    "POST", "/admin/recognitions", json=references
                )
            except VoiceUnavailable as exc:
                unavailable = str(exc)
        for review in listing["rows"]:
            review["pair"] = comparisons.get(
                review["reference"],
                [
                    {
                        "name": "GigaAM",
                        "text": review["transcript"] if review["status"] == "transcribed" else None,
                        "state": "unavailable" if unavailable else "not_scheduled",
                    },
                    {
                        "name": "Parakeet",
                        "text": None,
                        "state": "unavailable" if unavailable else "not_scheduled",
                    },
                ],
            )
        return render(
            request,
            "voice_admin.html",
            {
                "active_section": "reviews",
                "feed": listing,
                "q": query,
                "voice_unavailable": unavailable,
                "page_query": lambda n: urlencode({**({"q": query} if query else {}), "page": n}),
            },
        )

    @router.get(base)
    def index(request: Request, page: int = 1) -> Response:
        return show(request, page=page)

    @router.get(base + "/{slug}")
    def city(request: Request, slug: str, page: int = 1) -> Response:
        return show(request, slug, page)

    @router.get(base + "/{slug}/{number:int}")
    def numbered(request: Request, slug: str, number: int) -> Response:
        require_admin(request)
        read = VoiceReadModel(context(request).engine)
        identity, _ = read.resolve(slug, number)
        with context(request).engine.connect() as conn:
            from involta_voice.db import row

            offset = row(
                conn,
                """SELECT count(*) n FROM reviews r JOIN reviews selected ON selected.id=:id
                WHERE r.location_id=selected.location_id AND (r.created_at,r.number)>(selected.created_at,selected.number)""",
                id=identity,
            )
        page = offset["n"] // 20 + 1 if offset else 1
        return RedirectResponse(
            base + "/" + slug + "?page=" + str(page) + "#review-" + str(number), status_code=303
        )

    @router.get(prefix + "/logs/reviews/{slug}/{number:int}")
    def log(request: Request, slug: str, number: int) -> Response:
        require_admin(request)
        read = VoiceReadModel(context(request).engine)
        identity, _ = read.resolve(slug, number)
        detail = read.detail(identity)
        remote = comparison = None
        unavailable = ""
        if detail["link"]:
            reference = str(detail["link"]["reference"])
            try:
                remote = voice_client(request).json("GET", "/admin/submissions/" + reference)
                comparison = voice_client(request).json(
                    "GET", "/admin/submissions/" + reference + "/asr"
                )
            except VoiceUnavailable as exc:
                unavailable = str(exc)
        return render(
            request,
            "voice_log.html",
            {
                "active_section": "reviews",
                "v": jsonable_encoder(detail),
                "remote": remote,
                "comparison": comparison,
                "voice_unavailable": unavailable,
                "consumer_health": read.consumer_health(),
            },
        )

    @router.get(base + "/{slug}/{number:int}/audio")
    def audio(request: Request, slug: str, number: int) -> Response:
        require_admin(request)
        read = VoiceReadModel(context(request).engine)
        identity, _ = read.resolve(slug, number)
        link = read.detail(identity)["link"]
        if not link:
            raise HTTPException(404, "Аудио недоступно")
        remote = voice_client(request).request(
            "GET", "/admin/submissions/" + str(link["reference"]) + "/audio"
        )
        return Response(
            remote.content,
            status_code=remote.status_code,
            media_type=remote.headers.get("content-type", "application/octet-stream"),
            headers={"Cache-Control": "private,no-store"},
        )

    @router.post(base + "/inbox/{event_id}/retry")
    def retry_delivery(
        request: Request, event_id: UUID, form: FormData = Depends(form_data)
    ) -> Response:
        require_admin(request)
        check_csrf(request, form)
        from involta_guide.infrastructure.voice.consumer import Consumer

        Consumer(context(request).engine, voice_client(request)).retry(str(event_id))
        return RedirectResponse(base, status_code=303)

    return router
