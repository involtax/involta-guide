from datetime import timedelta, timezone
from hashlib import sha256
from textwrap import shorten
from urllib.parse import urlencode
from uuid import UUID
from xml.sax.saxutils import escape

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response

from involta_guide.domain.hours import DAYS, opening_status
from involta_guide.domain.rules import KINDS
from involta_guide.infrastructure.ai.audit import AIAudit
from involta_guide.infrastructure.voice.read_model import VoiceReadModel
from involta_guide.infrastructure.voice.upload_logs import UploadLogs
from involta_guide.web.http import admin_token, check_origin, context, require_diagnostics
from involta_guide.web.routes.admin import render


def routes(prefix: str) -> APIRouter:
    router = APIRouter()

    @router.get(prefix)
    def slash(request: Request) -> Response:
        target = context(request).settings.path("/")
        if request.url.query:
            target += "?" + request.url.query
        return RedirectResponse(
            target,
            status_code=302,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    @router.get(prefix + "/")
    def directory(request: Request) -> Response:
        ctx = context(request)
        if request.url.query:
            return RedirectResponse(ctx.settings.path("/"), status_code=308)
        request.state.audience_page = True
        return request.app.state.templates.TemplateResponse(
            request=request,
            name="directory.html",
            context={
                "settings": ctx.settings,
                "home_reviews": VoiceReadModel(ctx.engine).latest_public_texts(),
                "review_timezone": timezone(timedelta(hours=3)),
                "months": (
                    "янв",
                    "фев",
                    "мар",
                    "апр",
                    "мая",
                    "июн",
                    "июл",
                    "авг",
                    "сен",
                    "окт",
                    "ноя",
                    "дек",
                ),
                "kinds": KINDS,
                "indexable": True,
            },
        )

    @router.get(prefix + "/api/locations/catalogue")
    def location_catalogue(request: Request) -> Response:
        rows = context(request).queries.public_location_catalogue()
        response = JSONResponse(
            [
                {
                    "name": row["name"],
                    "location": row["slug"],
                    "context": " · ".join(
                        dict.fromkeys(
                            value
                            for value in (
                                KINDS[row["kind"]] if row["kind"] != "city" else None,
                                row["parent_name"],
                                row["region"],
                            )
                            if value
                        )
                    ),
                }
                for row in rows
            ]
        )
        etag = '"' + sha256(response.body).hexdigest() + '"'
        headers = {
            "ETag": etag,
            "Cache-Control": "public, max-age=0, must-revalidate",
            "X-Robots-Tag": "noindex",
        }
        validators = [
            value.strip().removeprefix("W/")
            for value in request.headers.get("if-none-match", "").split(",")
        ]
        if etag in validators or "*" in validators:
            return Response(status_code=304, headers=headers)
        response.headers.update(headers)
        return response

    @router.get(prefix + "/about")
    def about(request: Request) -> Response:
        ctx = context(request)
        request.state.audience_page = True
        response = request.app.state.templates.TemplateResponse(
            request=request,
            name="about.html",
            context={
                "settings": ctx.settings,
                "title": "О проекте — Инвольта гид",
                "description": "Короткие подборки ресторанов и голосовые отзывы гостей.",
                "canonical": ctx.settings.url("/about"),
                "indexable": True,
            },
        )
        response.set_cookie(
            "guide_about_seen",
            "1",
            max_age=365 * 86400,
            path=ctx.settings.base_path,
            httponly=True,
            secure=ctx.settings.cookie_secure,
            samesite="lax",
        )
        return response

    @router.get(prefix + "/robots.txt")
    def robots(request: Request) -> Response:
        s = context(request).settings
        return PlainTextResponse(
            f"User-agent: *\nAllow: {s.path('/')}\nDisallow: {s.path('/admin')}\nDisallow: {s.path('/api')}\nDisallow: {s.path('/alex')}\nDisallow: {s.path('/logs')}\nSitemap: {s.url('/sitemap.xml')}\n"
        )

    @router.get(prefix + "/sitemap.xml")
    def sitemap(request: Request) -> Response:
        ctx = context(request)
        rows = ctx.queries.sitemap()
        body = (
            '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>'
            + escape(ctx.settings.url("/"))
            + "</loc></url><url><loc>"
            + escape(ctx.settings.url("/about"))
            + "</loc></url>"
        )
        body += (
            "".join(
                "<url><loc>" + escape(ctx.settings.url("/" + row["slug"])) + "</loc></url>"
                for row in rows
            )
            + "</urlset>"
        )
        return Response(body, media_type="application/xml")

    # TEMPORARY public test controls; explicitly no login or privileged session required.
    log_headers = {
        "Cache-Control": "no-store",
        "X-Robots-Tag": "noindex, nofollow",
        "X-Content-Type-Options": "nosniff",
    }

    def check_log_access(request: Request) -> None:
        require_diagnostics(request)

    def log_store(request: Request) -> AIAudit:
        check_log_access(request)
        ctx = context(request)
        return AIAudit(ctx.engine, ctx.settings)

    def log_page(
        request: Request, job_id: str | None, q: str, page: int, status: str = ""
    ) -> Response:
        store = log_store(request)
        ctx = context(request)
        admin = bool(ctx.security.principal(admin_token(request)))
        listing = store.listing(page, q, status, public_only=not admin)
        if not job_id and listing["rows"]:
            job_id = str(listing["rows"][0]["id"])
        detail = store.detail(job_id, public_only=not admin) if job_id else None
        if job_id and detail is None:
            raise HTTPException(404, "Публичный журнал не найден")
        response = render(
            request,
            "logs.html",
            {
                "admin_ui": admin,
                "admin_authenticated": admin,
                "fragment": False,
                "logs_admin": admin,
                "audit_public": not admin,
                "audit_detail": detail,
                "log_location": ctx.queries.location(detail["job"]["location_id"])["location"]
                if admin and detail
                else None,
                "audit_jobs": listing,
                "log_kind": "fill",
                "q": q,
                "status": status,
                "active_section": "logs",
                "title": "Логи — Инвольта гид",
                "message": request.query_params.get("message", ""),
                "audit_page": lambda n: urlencode({"q": q, "status": status, "page": n}),
            },
        )
        response.headers.update({**log_headers, "Vary": "Cookie"})
        return response

    @router.get(prefix + "/logs")
    def public_logs(
        request: Request, q: str = "", page: int = 1, status: str = "", kind: str = ""
    ) -> Response:
        if kind == "uploads":
            return upload_log_page(request, None, q, page, status)
        return log_page(request, None, q, page, status)

    def upload_log_page(
        request: Request, request_id: str | None, q: str, page: int, status: str
    ) -> Response:
        check_log_access(request)
        ctx = context(request)
        admin = bool(ctx.security.principal(admin_token(request)))
        store = UploadLogs(ctx.engine, ctx.settings)
        listing = store.listing(page, q, status)
        selected = request_id is not None
        if not request_id and listing["rows"]:
            request_id = listing["rows"][0]["request_id"]
        detail = store.detail(request_id, admin=admin) if request_id else None
        if request_id and detail is None:
            raise HTTPException(404, "Журнал отправки не найден")
        if selected and detail:
            canonical = ctx.settings.path(detail["request"]["log_path"])
            if request.url.path != canonical:
                return RedirectResponse(canonical, status_code=308, headers=log_headers)
        response = render(
            request,
            "logs.html",
            {
                "admin_ui": admin,
                "admin_authenticated": admin,
                "fragment": False,
                "logs_admin": admin,
                "audit_public": not admin,
                "log_kind": "uploads",
                "upload_requests": listing,
                "upload_detail": detail,
                "q": q,
                "status": status,
                "active_section": "logs",
                "title": "Загрузки голоса — Логи — Инвольта гид",
                "audit_page": lambda n: urlencode(
                    {"kind": "uploads", "q": q, "status": status, "page": n}
                ),
            },
        )
        response.headers.update({**log_headers, "Vary": "Cookie"})
        return response

    @router.get(prefix + "/logs/{slug}/uploads/{request_id}.json")
    def upload_log_json(request: Request, slug: str, request_id: UUID) -> Response:
        check_log_access(request)
        ctx = context(request)
        detail = UploadLogs(ctx.engine, ctx.settings).detail(
            str(request_id), admin=bool(ctx.security.principal(admin_token(request)))
        )
        if detail is None:
            raise HTTPException(404, "Журнал отправки не найден")
        canonical = ctx.settings.path(detail["request"]["log_path"])
        if request.url.path != canonical + ".json":
            return RedirectResponse(canonical + ".json", status_code=308, headers=log_headers)
        headers = {**log_headers, "Vary": "Cookie"}
        if request.query_params.get("download") == "1":
            headers["Content-Disposition"] = (
                f'attachment; filename="voice-upload-{request_id}.json"'
            )
        return JSONResponse(detail, headers=headers)

    @router.get(prefix + "/logs/{slug}/uploads/{request_id}")
    def upload_log(
        request: Request, slug: str, request_id: UUID, q: str = "", page: int = 1, status: str = ""
    ) -> Response:
        return upload_log_page(request, str(request_id), q, page, status)

    def numbered_detail(request: Request, slug: str, number: int) -> dict:
        store = log_store(request)
        public_only = not bool(context(request).security.principal(admin_token(request)))
        job_id = store.resolve(slug, number, public_only=public_only) if number > 0 else None
        detail = store.detail(job_id, public_only=public_only) if job_id else None
        if detail is None:
            raise HTTPException(404, "Запрос не найден")
        return detail

    @router.get(prefix + "/logs/{slug}/{number}.json")
    def numbered_json(request: Request, slug: str, number: int) -> Response:
        detail = numbered_detail(request, slug, number)
        if slug != detail["job"]["location_slug"]:
            suffix = "?download=1" if request.query_params.get("download") == "1" else ""
            return RedirectResponse(
                context(request).settings.path(detail["job"]["log_path"]) + ".json" + suffix,
                status_code=308,
                headers=log_headers,
            )
        headers = dict(log_headers)
        if request.query_params.get("download") == "1":
            headers["Content-Disposition"] = (
                f'attachment; filename="log-{detail["job"]["location_slug"]}-{number}.json"'
            )
        return JSONResponse(detail, headers={**headers, "Vary": "Cookie"})

    @router.get(prefix + "/logs/{slug}/{number}/status")
    def log_status(request: Request, slug: str, number: int) -> Response:
        store = log_store(request)
        job = store.status(
            slug,
            number,
            public_only=not bool(context(request).security.principal(admin_token(request))),
        )
        if job is None:
            raise HTTPException(404, "Журнал не найден")
        return JSONResponse({"job": job}, headers={**log_headers, "Vary": "Cookie"})

    @router.get(prefix + "/logs/{slug}/{number}")
    def numbered_log(
        request: Request, slug: str, number: int, page: int = 1, q: str = "", status: str = ""
    ) -> Response:
        detail = numbered_detail(request, slug, number)
        canonical = context(request).settings.path(detail["job"]["log_path"])
        if slug != detail["job"]["location_slug"]:
            return RedirectResponse(canonical, status_code=308, headers=log_headers)
        return log_page(request, detail["job"]["id"], q, page, status)

    @router.get(prefix + "/logs/{slug}")
    def city_logs(request: Request, slug: str, page: int = 1, status: str = "") -> Response:
        city = context(request).queries.public(slug)
        if city is None:
            raise HTTPException(404, "Локация не найдена")
        return log_page(request, None, slug, page, status)

    @router.post(prefix + "/api/locations/{slug}/fill")
    def public_fill(request: Request, slug: str) -> Response:
        ctx = context(request)
        if not ctx.settings.temporary_public_fill_enabled:
            raise HTTPException(404, "Наполнение отключено")
        check_origin(request)
        data = ctx.queries.public(slug)
        if data is None:
            raise HTTPException(404, "Локация не найдена")
        state = ctx.jobs.enqueue_public_fill(data["location"]["id"])
        if "application/json" not in request.headers.get("accept", ""):
            return RedirectResponse(
                ctx.settings.path("/" + data["location"]["slug"]), status_code=303
            )
        return JSONResponse(
            state,
            status_code=202 if state["state"] in ("queued", "running") else 200,
            headers={"Cache-Control": "no-store"},
        )

    @router.get(prefix + "/api/locations/{slug}/fill/status")
    def public_fill_status(request: Request, slug: str) -> Response:
        ctx = context(request)
        if not ctx.settings.temporary_public_fill_enabled:
            raise HTTPException(404, "Наполнение отключено")
        data = ctx.queries.public(slug)
        if data is None:
            raise HTTPException(404, "Локация не найдена")
        return JSONResponse(
            ctx.jobs.public_fill_state(data["location"]["id"]),
            headers={"Cache-Control": "no-store"},
        )

    @router.get(prefix + "/{slug}")
    def location(request: Request, slug: str) -> Response:
        ctx = context(request)
        data = ctx.queries.public(slug)
        if data is None:
            return request.app.state.templates.TemplateResponse(
                request=request,
                name="error.html",
                status_code=404,
                context={
                    "settings": ctx.settings,
                    "title": "Локация не найдена",
                    "message": "Проверьте адрес или найдите локацию в справочнике.",
                },
            )
        c = data["location"]
        if data["redirect"]:
            return RedirectResponse(ctx.settings.path("/" + c["slug"]), status_code=308)
        public_reviews = VoiceReadModel(ctx.engine).public_texts(c["id"])
        request.state.audience_page = True
        title = "Где поесть " + (c["in_phrase"] or "в " + c["name"])
        description = (
            title + ": короткая подборка ресторанов, кухня и причины зайти. " + c["region"]
        )
        preview = shorten(data["content"].get("intro") or "", width=200, placeholder="…")
        breadcrumbs = [{"name": "Все локации", "slug": ""}, *data["parents"], c]
        structured = [
            {
                "@context": "https://schema.org",
                "@type": "BreadcrumbList",
                "itemListElement": [
                    {
                        "@type": "ListItem",
                        "position": i + 1,
                        "name": b["name"],
                        "item": ctx.settings.url("/" + b["slug"]),
                    }
                    for i, b in enumerate(breadcrumbs)
                ],
            }
        ]
        if data["content"]["restaurants"]:
            structured.append(
                {
                    "@context": "https://schema.org",
                    "@type": "ItemList",
                    "name": title,
                    "itemListElement": [
                        {
                            "@type": "ListItem",
                            "position": i + 1,
                            "item": {
                                "@type": "Restaurant",
                                "name": r["name"],
                                "servesCuisine": r["cuisine"],
                                "url": r["mapsUrl"],
                                "telephone": r.get("phone", ""),
                                "address": {
                                    "@type": "PostalAddress",
                                    "addressLocality": r.get("locationName") or c["name"],
                                    "addressCountry": "RU",
                                },
                            },
                        }
                        for i, r in enumerate(data["content"]["restaurants"])
                    ],
                }
            )
        return request.app.state.templates.TemplateResponse(
            request=request,
            name="location.html",
            context={
                **data,
                "settings": ctx.settings,
                "title": title + " — Инвольта гид",
                "description": description,
                "og_description": preview or description,
                "canonical": ctx.settings.url("/" + c["slug"]),
                "kinds": KINDS,
                "days": DAYS,
                "opening_status": opening_status,
                "public_reviews": public_reviews,
                "temporary_fill": ctx.jobs.public_fill_state(c["id"])
                if ctx.settings.temporary_public_fill_enabled
                and not c["is_hub"]
                and c["initialized_at"] is None
                else None,
                "structured": structured,
            },
            headers={"Cache-Control": "private, no-store"},
        )

    return router
