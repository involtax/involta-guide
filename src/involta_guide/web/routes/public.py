from urllib.parse import urlencode
from uuid import UUID
from xml.sax.saxutils import escape

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response

from involta_guide.domain.hours import DAYS, opening_status
from involta_guide.domain.rules import KINDS
from involta_guide.infrastructure.ai.audit import AIAudit
from involta_guide.web.http import admin_token, check_origin, context, require_admin
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
    def directory(request: Request, q: str = "", page: str = "1") -> Response:
        ctx = context(request)
        pairs = list(request.query_params.multi_items())
        if len(pairs) == 1 and not pairs[0][1]:
            token = ctx.security.alex_login(pairs[0][0])
            if token:
                response = RedirectResponse(
                    ctx.settings.path("/"),
                    status_code=302,
                    headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
                )
                response.set_cookie(
                    "guide_alex",
                    token,
                    max_age=30 * 86400,
                    path=prefix,
                    httponly=True,
                    secure=ctx.settings.cookie_secure,
                    samesite="strict",
                )
                return response
        try:
            page_number = int(page)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid page number") from exc
        searching = bool(q.strip())
        result = ctx.queries.directory(
            query=q,
            kind="" if searching else "city",
            page=page_number,
            status="active",
            filled_only=not searching,
            public_only=True,
        )
        total = (
            ctx.queries.directory(kind="city", status="active", filled_only=True, public_only=True)[
                "total"
            ]
            if searching
            else result["total"]
        )
        return request.app.state.templates.TemplateResponse(
            request=request,
            name="directory.html",
            context={
                "settings": ctx.settings,
                "q": q,
                "result": result,
                "total": total,
                "kinds": KINDS,
                "indexable": True,
                "page_query": lambda n: urlencode({"q": q, "page": n}),
            },
        )

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

    def log_store(request: Request) -> AIAudit:
        ctx = context(request)
        if not ctx.settings.public_fill_logs_enabled and not ctx.security.principal(
            admin_token(request)
        ):
            raise HTTPException(404, "Публичные логи отключены")
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
                "fragment": False,
                "logs_admin": admin,
                "audit_public": not admin,
                "audit_detail": detail,
                "log_location": ctx.queries.location(detail["job"]["location_id"])["location"]
                if admin and detail
                else None,
                "audit_jobs": listing,
                "q": q,
                "status": status,
                "tab": "logs",
                "title": "Логи — Инвольта гид",
                "message": request.query_params.get("message", ""),
                "audit_page": lambda n: urlencode({"q": q, "status": status, "page": n}),
            },
        )
        response.headers.update({**log_headers, "Vary": "Cookie"})
        return response

    @router.get(prefix + "/logs")
    def public_logs(request: Request, q: str = "", page: int = 1, status: str = "") -> Response:
        return log_page(request, None, q, page, status)

    def numbered_detail(request: Request, slug: str, number: int) -> dict:
        store = log_store(request)
        public_only = not bool(context(request).security.principal(admin_token(request)))
        job_id = store.resolve(slug, number, public_only=public_only) if number > 0 else None
        detail = store.detail(job_id, public_only=public_only) if job_id else None
        if detail is None:
            raise HTTPException(404, "Версия журнала не найдена")
        return detail

    @router.get(prefix + "/logs/history/{location_id}")
    def page_history(request: Request, location_id: UUID) -> Response:
        require_admin(request)
        selected = context(request).queries.location(str(location_id))
        return render(
            request,
            "page_history.html",
            {
                "selected": selected,
                "tab": "logs",
                "title": "История данных города",
            },
        )

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

    @router.get(prefix + "/logs/{slug}/{number}/audio")
    def log_audio(request: Request, slug: str, number: int) -> Response:
        require_admin(request)
        detail = numbered_detail(request, slug, number)
        if not detail["review"]:
            raise HTTPException(404, "Аудиозапись не найдена")
        data, mime = context(request).reviews.audio(detail["review"]["id"])
        return Response(data, media_type=mime, headers={"Cache-Control": "private, no-store"})

    @router.get(prefix + "/logs/{slug}/{number}")
    def numbered_log(
        request: Request, slug: str, number: int, page: int = 1, q: str = "", status: str = ""
    ) -> Response:
        detail = numbered_detail(request, slug, number)
        canonical = context(request).settings.path(detail["job"]["log_path"])
        if slug != detail["job"]["location_slug"]:
            return RedirectResponse(canonical, status_code=308, headers=log_headers)
        return log_page(request, detail["job"]["id"], q, page, status)

    @router.get(prefix + "/logs/{job_id}.json")
    def public_log_json(request: Request, job_id: UUID) -> Response:
        detail = log_store(request).detail(
            str(job_id),
            public_only=not bool(context(request).security.principal(admin_token(request))),
        )
        if detail is None:
            raise HTTPException(404, "Публичный журнал не найден")
        return RedirectResponse(
            context(request).settings.path(detail["job"]["log_path"]) + ".json",
            status_code=308,
            headers=log_headers,
        )

    @router.get(prefix + "/logs/{job_id}")
    def public_log(request: Request, job_id: str, page: int = 1) -> Response:
        try:
            identifier = str(UUID(job_id))
        except ValueError:
            return log_page(request, None, job_id, page)
        detail = log_store(request).detail(
            identifier,
            public_only=not bool(context(request).security.principal(admin_token(request))),
        )
        if detail is None:
            raise HTTPException(404, "Публичный журнал не найден")
        return RedirectResponse(
            context(request).settings.path(detail["job"]["log_path"]),
            status_code=308,
            headers=log_headers,
        )

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
        alex_token = request.cookies.get("guide_alex", "")
        alex = ctx.security.principal(alex_token, alex=True)
        title = "Где поесть " + (c["in_phrase"] or "в " + c["name"])
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
                "description": title
                + ": короткая подборка ресторанов, кухня и причины зайти. "
                + c["region"],
                "canonical": ctx.settings.url("/" + c["slug"]),
                "kinds": KINDS,
                "days": DAYS,
                "opening_status": opening_status,
                "alex": alex,
                "voice_csrf": ctx.security.csrf(alex_token) if alex else "",
                "temporary_fill": ctx.jobs.public_fill_state(c["id"])
                if ctx.settings.temporary_public_fill_enabled
                and not (c["published_version"] or c["draft_version"] or c["draft"])
                else None,
                "structured": structured,
            },
            headers={"Cache-Control": "private, no-store"},
        )

    return router
