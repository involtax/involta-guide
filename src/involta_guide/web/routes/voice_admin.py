from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import RedirectResponse, Response
from starlette.datastructures import FormData

from involta_guide.infrastructure.voice.client import VoiceUnavailable
from involta_guide.infrastructure.voice.read_model import VoiceReadModel
from involta_guide.web.http import admin_token, check_csrf, context, form_data, require_admin
from involta_guide.web.routes.admin import render
from involta_guide.web.routes.voice import voice_client
from involta_voice.variants import RESULTS, VARIANT_LABELS


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
                        "name": "GigaAM" if engine == "gigaam" else "Parakeet",
                        "variant_label": VARIANT_LABELS[variant],
                        "dictionary": None,
                        "text": review["transcript"]
                        if engine == "gigaam"
                        and variant == "onnx"
                        and review["status"] == "transcribed"
                        else None,
                        "state": "unavailable" if unavailable else "not_scheduled",
                    }
                    for engine, variant in RESULTS
                    if review["reference"] or variant == "onnx"
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
                WHERE r.status<>'deleted' AND r.location_id=selected.location_id AND (r.created_at,r.number)>(selected.created_at,selected.number)""",
                id=identity,
            )
        page = offset["n"] // 20 + 1 if offset else 1
        return RedirectResponse(
            base + "/" + slug + "?page=" + str(page) + "#review-" + str(number), status_code=303
        )

    @router.get(prefix + "/logs/reviews/{slug}/{number:int}")
    def log(request: Request, slug: str, number: int) -> Response:
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
        from involta_guide.infrastructure.voice.public_log import public_log

        settings = context(request).settings
        secrets = tuple(
            getattr(settings, name).get_secret_value()
            for name in (
                "openai_api_key",
                "session_secret",
                "voice_service_secret",
                "admin_password",
            )
        )
        return render(
            request,
            "voice_log.html",
            {
                "active_section": "reviews",
                "v": public_log(jsonable_encoder(detail), secrets),
                "remote": public_log(remote, secrets),
                "comparison": public_log(comparison, secrets),
                "voice_unavailable": unavailable,
                "consumer_health": read.consumer_health(),
                "show_admin_nav": context(request).security.principal(admin_token(request))
                is not None,
                "workspace_boost": False,
            },
        )

    @router.post(base + "/{slug}/{number:int}/delete")
    def delete(
        request: Request, slug: str, number: int, form: FormData = Depends(form_data)
    ) -> Response:
        principal = require_admin(request)
        check_csrf(request, form)
        from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow
        from involta_voice.db import encoded, row

        ctx = context(request)
        identity, _ = VoiceReadModel(ctx.engine).resolve(slug, number)
        with PostgresUow(ctx.engine) as uow:
            removed = row(
                uow.session.connection(),
                "UPDATE reviews SET status='deleted',publication_status=0 WHERE id=:id AND status<>'deleted' RETURNING id",
                id=identity,
            )
            if removed:
                uow.repo.audit(
                    "review_deleted",
                    identity,
                    encoded({"actor": principal["id"], "slug": slug, "number": number}),
                )
        return RedirectResponse(base + "/" + slug, status_code=303)

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
