"""Explicit routes for the current administrative content, without legacy aliases."""

import json
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import FormData

from involta_guide.application.commands.schemas import LocationInput
from involta_guide.catalogue.service import PROMPTS, CityContent, digest
from involta_guide.domain.entities import Conflict, RuleViolation
from involta_guide.domain.location_search import search_locations
from involta_guide.domain.location_slug import location_slug
from involta_guide.domain.rules import KINDS
from involta_guide.infrastructure.location_catalog import export_catalog
from involta_guide.web.http import check_csrf, context, form_data, require_admin

from .admin import key, log_redirect, render


def number_param(request: Request, name: str = "page") -> int:
    try:
        return max(1, int(request.query_params.get(name, "1")))
    except ValueError:
        raise HTTPException(400, "Некорректный номер страницы") from None


def location_values(form: FormData, *, new: bool = False) -> dict[str, Any]:
    fields: dict[str, Any] = {
        name: key(form, name)
        for name in (
            "name",
            "region",
            "slug",
            "in_phrase",
            "kind",
            "priority",
            "coverage_note",
        )
    }
    if new and not fields["slug"]:
        fields["slug"] = location_slug(fields["name"])
    fields.update(
        parent_id=int(key(form, "parent_id")) if key(form, "parent_id") else None,
        aliases=[v.strip() for v in key(form, "aliases").split(",") if v.strip()],
        top_limit=int(key(form, "top_limit") or 3),
        bounds=json.loads(key(form, "bounds")) if key(form, "bounds") else None,
    )
    for name in ("is_hub", "listed", "population_verified"):
        fields[name] = form.get(name) == "on"
    fields["active"] = int(form.get("active") == "on")
    for name in ("population", "latitude", "longitude"):
        fields[name] = float(key(form, name)) if key(form, name) else None
    return LocationInput.model_validate(fields).model_dump(mode="json")


def location_page(
    request: Request,
    slug: str | None = None,
    *,
    new: bool = False,
    message: str = "",
    entered_intro: str | None = None,
    entered_settings: dict[str, Any] | None = None,
    nearby_results: list[dict[str, Any]] | None = None,
) -> Response:
    ctx = context(request)
    params = request.query_params
    status = params.get("status", "all")
    if status not in ("all", "empty", "filled"):
        raise HTTPException(400, "Неизвестный фильтр")
    listing = ctx.queries.directory(
        query=params.get("q", ""),
        status="published" if status == "filled" else status,
        page=number_param(request),
        admin_order=True,
    )
    selected = (
        ctx.queries.admin_location(slug)
        if slug
        else ({"location": None, "nearby": [], "intro_events": []} if new else None)
    )
    choices = ctx.queries.rows("locations", limit=100000)
    current_id = selected["location"]["id"] if selected and selected["location"] else None
    nearby_catalogue = [
        {
            "id": row["id"],
            "name": row["name"],
            "context": KINDS[row["kind"]] + " · " + row["region"],
            "url": ctx.settings.path("/" + row["slug"]),
        }
        for row in sorted(choices, key=lambda row: (row["name"], row["id"]))
        if row["id"] != current_id
    ]
    pending = []
    if current_id:
        with ctx.jobs.uow() as work:
            pending = [
                row
                for row in work.repo.rows(
                    "jobs", {"location_id": current_id, "kind": "generate"}, limit=10000
                )
                if row["status"] in ("queued", "running")
            ]
    return render(
        request,
        "admin.html",
        {
            "active_section": "locations",
            "intro_expected": digest(selected["location"]["intro"])
            if selected and selected["location"]
            else "",
            "content_pending": [row["report"].get("operation") for row in pending if row["report"]],
            "message": message or params.get("message", ""),
            "q": params.get("q", ""),
            "status": status,
            "location_cities": listing["rows"],
            "listing": listing,
            "selected": selected,
            "location_form": entered_settings,
            "new_location": new,
            "location_choices": choices,
            "nearby_catalogue": nearby_catalogue,
            **({"nearby_results": nearby_results} if nearby_results is not None else {}),
            "entered_intro": entered_intro,
        },
    )


def restaurant_page(request: Request, slug: str | None = None) -> Response:
    require_admin(request)
    ctx = context(request)
    location_id = ctx.queries.admin_location(slug)["location"]["id"] if slug else None
    data = ctx.queries.restaurant_browser(request.query_params.get("q", ""), location_id)
    cards = data["restaurant_cards"]
    with ctx.jobs.uow() as work:
        active = {
            row["report"].get("restaurant_id")
            for row in work.repo.rows(
                "jobs",
                {"kind": "generate", **({"location_id": location_id} if location_id else {})},
                limit=10000,
            )
            if row["status"] in ("queued", "running") and row["report"]
        }
    for card in cards:
        card["content_pending"] = card["id"] in active
    pages = max(1, (len(cards) + 19) // 20)
    page = min(pages, number_param(request))
    data["restaurant_cards"] = cards[(page - 1) * 20 : page * 20]
    data["listing"] = {"page": page, "pages": pages, "total": len(cards)}
    return render(
        request,
        "admin.html",
        {
            "active_section": "restaurants",
            "message": "",
            "q": request.query_params.get("q", ""),
            **data,
        },
    )


def filling_prompts(request: Request) -> dict[str, str]:
    from involta_guide.catalogue.service import ROOT

    ctx = context(request)
    return {
        op: ctx.queries.setting("content_" + op + "_prompt")
        or (ROOT / "data/prompts" / filename).read_text()
        for op, filename in PROMPTS.items()
    }


def routes(prefix: str) -> APIRouter:
    router = APIRouter()

    @router.get(prefix + "/admin/locations")
    def locations(request: Request) -> Response:
        require_admin(request)
        return location_page(request)

    @router.get(prefix + "/admin/locations/new")
    def new_location(request: Request) -> Response:
        require_admin(request)
        return location_page(request, new=True)

    @router.get(prefix + "/admin/locations/export")
    def export(request: Request) -> Response:
        require_admin(request)
        return JSONResponse(
            jsonable_encoder(
                export_catalog(
                    context(request).settings.root_dir,
                    context(request).queries.rows("locations", limit=100000),
                    context(request).queries.rows("location_slug_redirects", limit=100000),
                )
            ),
            headers={"Cache-Control": "private, no-store"},
        )

    @router.post(prefix + "/admin/locations/{slug}/nearby/{action}")
    def nearby(
        request: Request, slug: str, action: str, form: FormData = Depends(form_data)
    ) -> Response:
        require_admin(request)
        check_csrf(request, form)
        if action not in {"add", "remove"}:
            raise HTTPException(404)
        ctx = context(request)
        current = ctx.queries.admin_location(slug)["location"]
        if action == "add" and not key(form, "nearby_id"):
            choices = ctx.queries.rows("locations", limit=100000)
            matches = search_locations(
                sorted(choices, key=lambda row: (row["name"], row["id"])), key(form, "q")
            )
            return location_page(
                request,
                slug,
                nearby_results=[row for row in matches if row["id"] != current["id"]][:6],
            )
        try:
            other = int(key(form, "nearby_id"))
            ctx.locations.nearby(current["id"], other, remove=action == "remove")
        except (ValueError, RuleViolation) as exc:
            return location_page(request, slug, message=str(exc))
        return log_redirect(
            request,
            "/admin/locations/" + slug + "?" + urlencode({"message": "Список «Рядом» сохранён"}),
        )

    @router.get(prefix + "/admin/locations/{slug}")
    def location(request: Request, slug: str) -> Response:
        require_admin(request)
        return location_page(request, slug)

    def save_location(request: Request, slug: str | None, form: FormData) -> Response:
        require_admin(request)
        check_csrf(request, form)
        ctx = context(request)
        current = ctx.queries.admin_location(slug)["location"] if slug else None
        try:
            values = location_values(form, new=current is None)
            ctx.locations.save(current["id"] if current else None, values)
        except ValueError as exc:
            # Keep submitted values for correction, without persisting a partial update.
            submitted = {
                **(current or {}),
                **dict(form),
                "aliases": key(form, "aliases").split(","),
                "bounds": None,
            }
            return location_page(
                request, slug, new=not slug, message=str(exc), entered_settings=submitted
            )
        return log_redirect(
            request,
            "/admin/locations/"
            + values["slug"]
            + "?"
            + urlencode({"message": "Локация сохранена"}),
        )

    @router.post(prefix + "/admin/locations")
    def create_location(request: Request, form: FormData = Depends(form_data)) -> Response:
        return save_location(request, None, form)

    @router.post(prefix + "/admin/locations/{slug}")
    def update_location(
        request: Request, slug: str, form: FormData = Depends(form_data)
    ) -> Response:
        return save_location(request, slug, form)

    @router.post(prefix + "/admin/locations/{slug}/intro")
    def intro(request: Request, slug: str, form: FormData = Depends(form_data)) -> Response:
        principal = require_admin(request)
        check_csrf(request, form)
        ctx = context(request)
        selected = ctx.queries.admin_location(slug)
        text = str(form.get("intro") or "")
        try:
            changed = ctx.locations.save_intro(
                selected["location"]["id"], text, key(form, "expected") or None, principal["id"]
            )
        except RuleViolation as exc:
            response = location_page(request, slug, message=str(exc), entered_intro=text)
            # HTMX must render the preserved input just like a normal form submission.
            if not request.headers.get("HX-Request"):
                response.status_code = 409 if isinstance(exc, Conflict) else 400
            return response
        return log_redirect(
            request,
            "/admin/locations/"
            + slug
            + "?"
            + urlencode(
                {"message": "Вступление сохранено" if changed else "Вступление не изменилось"}
            ),
        )

    @router.post(prefix + "/admin/locations/{slug}/content/{operation}")
    def content_operation(
        request: Request, slug: str, operation: str, form: FormData = Depends(form_data)
    ) -> Response:
        require_admin(request)
        check_csrf(request, form)
        if operation not in ("intro", "discover"):
            raise HTTPException(404)
        ctx = context(request)
        location = ctx.queries.admin_location(slug)["location"]
        job_id = CityContent(ctx.jobs.uow, ctx.settings.openai_model).enqueue(
            location["id"], operation
        )
        with ctx.jobs.uow() as work:
            log = work.repo.rows("request_logs", {"job_id": job_id}, limit=1)[0]
        return log_redirect(request, f"/logs/{slug}/{log['number']}")

    @router.post(prefix + "/admin/restaurants/{number:int}/content/{action}")
    def restaurant_operation(
        request: Request, number: int, action: str, form: FormData = Depends(form_data)
    ) -> Response:
        require_admin(request)
        check_csrf(request, form)
        ctx = context(request)
        with ctx.jobs.uow() as work:
            records = work.repo.rows("restaurants", {"number": number}, limit=1)
            if not records:
                raise HTTPException(404)
            restaurant = records[0]
            from involta_guide.application.services.editorial import require

            location = require(work.repo, "locations", restaurant["location_id"])
        content = CityContent(ctx.jobs.uow, ctx.settings.openai_model)
        if action in ("hide", "show"):
            content.visibility(restaurant["id"], action == "hide")
            return log_redirect(request, "/admin/restaurants/" + location["slug"])
        if action not in ("all", "card", "assessment"):
            raise HTTPException(404)
        job_id = content.enqueue(location["id"], "research", restaurant["id"], action)
        with ctx.jobs.uow() as work:
            log = work.repo.rows("request_logs", {"job_id": job_id}, limit=1)[0]
        return log_redirect(request, f"/logs/{location['slug']}/{log['number']}")

    @router.get(prefix + "/admin/restaurants")
    def restaurants(request: Request) -> Response:
        return restaurant_page(request)

    @router.get(prefix + "/admin/restaurants/{number:int}")
    def restaurant(request: Request, number: int) -> Response:
        require_admin(request)
        if number <= 0:
            raise HTTPException(404)
        return render(
            request,
            "admin.html",
            {
                "active_section": "restaurants",
                "message": "",
                "q": "",
                "detail": True,
                **context(request).queries.restaurant(number),
            },
        )

    @router.get(prefix + "/admin/restaurants/{slug}")
    def city_restaurants(request: Request, slug: str) -> Response:
        return restaurant_page(request, slug)

    @router.get(prefix + "/admin/settings/filling")
    def filling(request: Request) -> Response:
        require_admin(request)
        return render(
            request,
            "admin.html",
            {
                "active_section": "fill",
                "message": request.query_params.get("message", ""),
                "content_prompts": filling_prompts(request),
            },
        )

    @router.post(prefix + "/admin/settings/filling")
    def save_filling(request: Request, form: FormData = Depends(form_data)) -> Response:
        require_admin(request)
        check_csrf(request, form)
        try:
            ctx = context(request)
            CityContent(ctx.jobs.uow).save_settings(
                {op: str(form.get(op) or "") for op in PROMPTS}, key(form, "model")
            )
        except RuleViolation as exc:
            return render(
                request,
                "admin.html",
                {
                    "active_section": "fill",
                    "message": str(exc),
                    "content_prompts": {op: str(form.get(op) or "") for op in PROMPTS},
                    "generation_model": key(form, "model"),
                },
            )
        return log_redirect(
            request, "/admin/settings/filling?" + urlencode({"message": "Настройки сохранены"})
        )

    @router.get(prefix + "/admin/sessions")
    def sessions(request: Request) -> Response:
        require_admin(request)
        return render(
            request,
            "admin.html",
            {
                "active_section": "sessions",
                "message": "",
                "sessions": context(request).queries.rows("admin_sessions"),
            },
        )

    return router
