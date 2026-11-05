from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.datastructures import FormData

from involta_guide.infrastructure.voice.client import VoiceUnavailable
from involta_guide.infrastructure.voice.read_model import VoiceReadModel
from involta_guide.web.http import (
    admin_token,
    check_csrf,
    context,
    form_data,
    require_admin,
    require_diagnostics,
)
from involta_guide.web.routes.admin import render
from involta_guide.web.routes.voice import current_location_context, voice_client
from involta_voice.variants import PRIMARY_VARIANT, RESULTS, VARIANT_LABELS


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
            review["reprocess_key"] = str(uuid4())
            review["pair"] = comparisons.get(
                review["reference"],
                [
                    {
                        "name": "GigaAM",
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
                    if variant == PRIMARY_VARIANT
                    if review["reference"]
                ],
            )
            if not review["reference"]:
                review["pair"] = [
                    {"name": "GigaAM", "text": review["transcript"], "state": "completed"}
                ]
            review["pair"] = [
                candidate for candidate in review["pair"] if candidate.get("name") == "GigaAM"
            ]
            review["recognizing"] = any(
                c.get("primary") and c.get("state") in ("queued", "running") for c in review["pair"]
            )
            review["reprocessing"] = review["recognizing"] and any(
                c.get("primary") and c.get("run", 1) > 1 for c in review["pair"]
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

    diagnostic_headers = {
        "Cache-Control": "no-store",
        "X-Robots-Tag": "noindex, nofollow",
        "X-Content-Type-Options": "nosniff",
    }

    def review_detail(request: Request, slug: str, number: int) -> dict[str, Any]:
        admin = require_diagnostics(request)
        read = VoiceReadModel(context(request).engine)
        identity, _ = read.resolve(slug, number)
        detail = read.detail(identity)
        if detail["review"]["status"] == "deleted" and not admin:
            raise HTTPException(404, "Отзыв удалён")
        return detail

    @router.get(prefix + "/logs/reviews.json")
    def export_index(
        request: Request, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)
    ) -> Response:
        require_diagnostics(request)
        settings = context(request).settings
        rows = VoiceReadModel(context(request).engine).retained(after, limit + 1)
        more = len(rows) > limit
        rows = rows[:limit]
        for item in rows:
            path = "/logs/reviews/" + item["slug"] + "/" + str(item["number"])
            item["log_url"] = settings.path(path)
            item["json_url"] = settings.path(path + ".json")
            item["audio_url"] = settings.path(path + "/audio") if item["reference"] else None
        return JSONResponse(
            jsonable_encoder(
                {
                    "schema_version": 1,
                    "reviews": rows,
                    "next_after": rows[-1]["number"] if more else None,
                }
            ),
            headers=diagnostic_headers,
        )

    def diagnostic_data(request: Request, slug: str, number: int) -> dict[str, Any]:
        detail = review_detail(request, slug, number)
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
        content = public_log(
            jsonable_encoder(
                {
                    "schema_version": 1,
                    "detail": detail,
                    "voice": remote,
                    "asr": comparison,
                    "unavailable": unavailable,
                    "consumer_health": VoiceReadModel(context(request).engine).consumer_health(),
                }
            ),
            secrets,
        )
        path = "/logs/reviews/" + slug + "/" + str(number)
        content["audio_url"] = settings.path(path + "/audio") if detail["link"] else None
        return content

    @router.get(prefix + "/logs/reviews/{slug}/{number:int}")
    def log(request: Request, slug: str, number: int) -> Response:
        data = diagnostic_data(request, slug, number)
        response = render(
            request,
            "voice_log.html",
            {
                "active_section": "reviews",
                "v": data["detail"],
                "remote": data["voice"],
                "comparison": data["asr"],
                "voice_unavailable": data["unavailable"],
                "consumer_health": data["consumer_health"],
                "show_admin_nav": context(request).security.principal(admin_token(request))
                is not None,
                "workspace_boost": False,
            },
        )
        response.headers.update(diagnostic_headers)
        return response

    @router.get(prefix + "/logs/reviews/{slug}/{number:int}.json")
    def export_detail(request: Request, slug: str, number: int) -> Response:
        return JSONResponse(diagnostic_data(request, slug, number), headers=diagnostic_headers)

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

    @router.post(base + "/{slug}/{number:int}/reprocess")
    def reprocess(
        request: Request, slug: str, number: int, form: FormData = Depends(form_data)
    ) -> Response:
        principal = require_admin(request)
        check_csrf(request, form)
        from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow
        from involta_voice.contracts import ReprocessRequest
        from involta_voice.db import encoded

        ctx = context(request)
        read = VoiceReadModel(ctx.engine)
        identity, _ = read.resolve(slug, number)
        detail = read.detail(identity)
        if detail["review"]["status"] == "deleted" or not detail["link"]:
            raise HTTPException(404, "Исходный отзыв недоступен")
        location = detail["location"]
        if (
            location["reset_pending"]
            or detail["review"]["content_epoch"] != location["content_epoch"]
        ):
            raise HTTPException(409, "Контекст отзыва устарел")
        try:
            key = UUID(str(form.get("idempotency_key") or ""))
        except ValueError:
            raise HTTPException(400, "Некорректный ключ перераспознавания") from None
        body = ReprocessRequest(
            location=current_location_context(request, location),
            idempotency_key=key,
        )
        result = voice_client(request).json(
            "POST",
            "/admin/submissions/" + str(detail["link"]["reference"]) + "/reprocess",
            json=body.model_dump(mode="json"),
            actor_session=principal["id"],
        )
        with PostgresUow(ctx.engine) as uow:
            uow.repo.audit(
                "review_reprocess",
                identity,
                encoded({"actor": principal["id"], "run": result["run"], "state": result["state"]}),
            )
        return RedirectResponse(base + "/" + slug + "/" + str(number), status_code=303)

    @router.get(prefix + "/logs/reviews/{slug}/{number:int}/audio")
    def audio(request: Request, slug: str, number: int, playback: bool = False) -> Response:
        link = review_detail(request, slug, number)["link"]
        if not link:
            raise HTTPException(404, "Аудио недоступно")
        remote = voice_client(request).request(
            "GET",
            "/admin/submissions/" + str(link["reference"]) + "/audio",
            params={"playback": str(playback).lower()},
            headers={
                name: request.headers[name]
                for name in ("range", "if-range")
                if name in request.headers
            },
        )
        forwarded = {
            name: remote.headers[name]
            for name in (
                "accept-ranges",
                "content-range",
                "content-length",
                "etag",
                "last-modified",
            )
            if name in remote.headers
        }
        return Response(
            remote.content,
            status_code=remote.status_code,
            media_type=remote.headers.get("content-type", "application/octet-stream"),
            headers={**diagnostic_headers, **forwarded},
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
