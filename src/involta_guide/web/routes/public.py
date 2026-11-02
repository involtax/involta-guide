from urllib.parse import urlencode
from xml.sax.saxutils import escape

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response

from involta_guide.domain.hours import DAYS, opening_status
from involta_guide.domain.rules import KINDS
from involta_guide.web.http import check_origin, context


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
    def directory(request: Request, q: str = "", kind: str = "", page: str = "1") -> Response:
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
        result = ctx.queries.directory(
            query=q, kind=kind, page=page_number, status="active", roots_only=True, public_only=True
        )
        return request.app.state.templates.TemplateResponse(
            request=request,
            name="directory.html",
            context={
                "settings": ctx.settings,
                "q": q,
                "kind": kind,
                "result": result,
                "total": ctx.queries.overview()["total"],
                "kinds": KINDS,
                "indexable": True,
                "page_query": lambda n: urlencode({"q": q, "kind": kind, "page": n}),
            },
        )

    @router.get(prefix + "/robots.txt")
    def robots(request: Request) -> Response:
        s = context(request).settings
        return PlainTextResponse(
            f"User-agent: *\nAllow: {s.path('/')}\nDisallow: {s.path('/admin')}\nDisallow: {s.path('/api')}\nDisallow: {s.path('/alex')}\nSitemap: {s.url('/sitemap.xml')}\n"
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
