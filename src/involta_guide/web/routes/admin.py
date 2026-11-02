import json
import secrets
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.datastructures import FormData

from involta_guide.application.commands.schemas import ContentInput, LocationInput, RestaurantInput
from involta_guide.domain.entities import RuleViolation
from involta_guide.domain.rules import FACTOR_LABELS, FACTORS, KINDS
from involta_guide.infrastructure.ai.audit import AIAudit
from involta_guide.web.http import (
    admin_token,
    check_csrf,
    client_ip,
    context,
    form_data,
    require_admin,
)


def key(form: FormData, name: str) -> str:
    return str(form.get(name) or "").strip()


def uid(value: str) -> str:
    return str(UUID(value))


def card_from_form(form: FormData, prefix: str = "") -> dict[str, Any]:
    def value(name: str) -> str:
        return key(form, prefix + name)

    data: dict[str, Any] = {
        name: value(name) for name in ("name", "cuisine", "description", "mapsUrl", "phone")
    }
    data["phone"] = "".join(c for c in data["phone"] if c not in " ()-")
    for name in ("id", "locationId"):
        if value(name):
            data[name] = uid(value(name))
    for name in ("latitude", "longitude"):
        data[name] = float(value(name)) if value(name) else None
    data["openingHours"] = json.loads(value("openingHours")) if value("openingHours") else None
    data["sources"] = [s.strip() for s in value("sources").splitlines() if s.strip()]
    return RestaurantInput.model_validate(data).model_dump(mode="json", exclude_none=True)


def render(request: Request, name: str, data: dict[str, Any]) -> Response:
    ctx = context(request)
    token = admin_token(request)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name=name,
        context={
            "settings": ctx.settings,
            "fragment": request.headers.get("HX-Request") == "true",
            "admin_ui": True,
            "csrf_token": ctx.security.csrf(token),
            "kinds": KINDS,
            "factors": FACTORS,
            "factor_labels": FACTOR_LABELS,
            "generation_model": ctx.jobs.generation_model(),
            **data,
        },
        headers={"Cache-Control": "private, no-store", "Vary": "HX-Request"},
    )


def redirect(request: Request, tab: str = "overview", message: str = "", **params: str) -> Response:
    return RedirectResponse(
        context(request).settings.path("/admin")
        + "?"
        + urlencode({"tab": tab, "message": message, **params}),
        status_code=303,
    )


def routes(prefix: str) -> APIRouter:
    router = APIRouter()

    @router.get(prefix + "/admin")
    def admin(request: Request) -> Response:
        ctx = context(request)
        if not ctx.security.principal(admin_token(request)):
            csrf_cookie = request.cookies.get("guide_login_csrf") or secrets.token_hex(32)
            response = render(
                request,
                "login.html",
                {
                    "message": request.query_params.get("message", ""),
                    "csrf_token": ctx.security.csrf(csrf_cookie),
                },
            )
            response.set_cookie(
                "guide_login_csrf",
                csrf_cookie,
                max_age=1800,
                path=prefix,
                httponly=True,
                secure=ctx.settings.cookie_secure,
                samesite="strict",
            )
            return response
        params = request.query_params
        tab = params.get("tab", "overview")
        if tab == "cities":
            tab = "locations"
        q = params.get("q", "")
        status = params.get("status", "all")
        data: dict[str, Any] = {
            "tab": tab,
            "message": params.get("message", ""),
            "overview": ctx.queries.overview(),
            "q": q,
            "status": status,
            "selected": None,
            "review_detail": None,
            "add_card": params.get("add") == "1",
        }
        if tab == "locations":
            data["locations"] = ctx.queries.directory(
                query=q, status=status, page=int(params.get("page", "1"))
            )
            location = params.get("location") or params.get("city")
            if location:
                data["selected"] = (
                    {"location": None, "related_ids": []}
                    if location == "new"
                    else ctx.queries.location(uid(location))
                )
            data["admin_page"] = lambda page: urlencode(
                {"tab": "locations", "q": q, "status": status, "page": page}
            )
        elif tab == "fill":
            data.update(
                prompt=ctx.queries.setting("prompt"),
                jobs=AIAudit(ctx.engine, ctx.settings).listing(1)["rows"],
            )
        elif tab == "reviews":
            moderation = params.get("moderation", "")
            data["moderation"] = moderation
            data["reviews"] = ctx.queries.rows(
                "reviews", {"moderation": moderation} if moderation else None
            )
            if params.get("review"):
                detail = ctx.queries.review(uid(params["review"]))
                data["review_detail"] = detail
                content = ctx.queries.location(detail["review"]["location_id"])["content"]
                data["proposal_before"] = {
                    p["id"]: next(
                        (
                            r
                            for r in content["restaurants"]
                            if r.get("id") == p["payload"].get("restaurantId")
                        ),
                        None,
                    )
                    for p in detail["proposals"]
                }
        elif tab == "restaurants":
            data["restaurants"] = ctx.queries.rows("restaurants", limit=1000)
        elif tab == "ratings":
            from involta_guide.domain.rules import aggregate_scores

            data["ratings"] = aggregate_scores(ctx.queries.rows("reviews", limit=10000))
        elif tab == "contacts":
            data["contacts"] = ctx.queries.contacts()
        elif tab == "sessions":
            data["sessions"] = ctx.queries.rows("admin_sessions")
        elif tab == "ai-audit":
            store = AIAudit(ctx.engine, ctx.settings)
            try:
                page = int(params.get("page", "1"))
            except ValueError:
                page = 1
            data["audit_jobs"] = store.listing(page, q, "" if status == "all" else status)
            data["audit_detail"] = None
            if params.get("job"):
                data["audit_detail"] = store.detail(uid(params["job"]))
                if data["audit_detail"] is None:
                    raise HTTPException(404, "Задание не найдено")
            data["audit_page"] = lambda page: urlencode(
                {"tab": "ai-audit", "q": q, "status": status, "page": page}
            )
        return render(request, "admin.html", data)

    @router.get(prefix + "/admin/ai-audit/{job_id}.json")
    def audit_download(request: Request, job_id: str) -> Response:
        require_admin(request)
        ctx = context(request)
        detail = AIAudit(ctx.engine, ctx.settings).detail(uid(job_id))
        if detail is None:
            raise HTTPException(404, "Задание не найдено")
        return JSONResponse(
            detail,
            headers={
                "Cache-Control": "private, no-store",
                "Content-Disposition": f'attachment; filename="ai-audit-{uid(job_id)}.json"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.post(prefix + "/admin/login")
    def login(request: Request, form: FormData = Depends(form_data)) -> Response:
        check_csrf(request, form, login=True)
        ctx = context(request)
        try:
            token = ctx.security.login(
                key(form, "password"), ctx.security.fingerprint(client_ip(request))
            )
        except RuleViolation as exc:
            return redirect(request, message=str(exc))
        response = RedirectResponse(ctx.settings.path("/admin"), status_code=303)
        response.set_cookie(
            "guide_admin",
            token,
            max_age=28800,
            path=prefix,
            httponly=True,
            secure=ctx.settings.cookie_secure,
            samesite="strict",
        )
        return response

    @router.post(prefix + "/admin/logout")
    def logout(request: Request, form: FormData = Depends(form_data)) -> Response:
        require_admin(request)
        check_csrf(request, form)
        context(request).security.revoke(admin_token(request))
        response = redirect(request)
        response.delete_cookie("guide_admin", path=prefix)
        return response

    @router.get(prefix + "/admin/jobs")
    def job_list(request: Request) -> Response:
        require_admin(request)
        ctx = context(request)
        return render(
            request, "jobs.html", {"jobs": AIAudit(ctx.engine, ctx.settings).listing(1)["rows"]}
        )

    @router.get(prefix + "/admin/card")
    def card(request: Request) -> Response:
        require_admin(request)
        template = request.app.state.templates.env.get_template("macros.html")
        return Response(template.module.restaurant({}, "r" + uuid4().hex), media_type="text/html")

    @router.get(prefix + "/admin/contacts")
    def contacts(request: Request) -> Response:
        require_admin(request)
        return redirect(request, "contacts")

    @router.post(prefix + "/admin/actions/{action}")
    def action(request: Request, action: str, form: FormData = Depends(form_data)) -> Response:
        require_admin(request)
        check_csrf(request, form)
        ctx = context(request)
        location = key(form, "location_id")
        expected = key(form, "expected") or None
        try:
            if action == "location":
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
                fields.update(
                    parent_id=key(form, "parent_id") or None,
                    aliases=[v.strip() for v in key(form, "aliases").split(",") if v.strip()],
                    top_limit=int(key(form, "top_limit") or 3),
                    related_ids=[
                        uid(v.strip()) for v in key(form, "related_ids").split(",") if v.strip()
                    ],
                    bounds=json.loads(key(form, "bounds")) if key(form, "bounds") else None,
                )
                for name in ("is_hub", "listed", "population_verified"):
                    fields[name] = form.get(name) == "on"
                fields["active"] = int(form.get("active") == "on")
                for name in ("population", "latitude", "longitude"):
                    fields[name] = float(key(form, name)) if key(form, name) else None
                validated = LocationInput.model_validate(fields).model_dump(mode="json")
                location = ctx.locations.save(
                    uid(key(form, "id")) if key(form, "id") else None, validated
                )
                return redirect(request, "locations", "Локация сохранена", location=location)
            if action == "content":
                content = {
                    "intro": key(form, "intro"),
                    "restaurants": [
                        card_from_form(form, str(k) + "_") for k in form.getlist("card")
                    ],
                }
                report = json.loads(key(form, "rankingReport") or "null")
                if report is not None:
                    content["rankingReport"] = report
                validated = ContentInput.model_validate(content).model_dump(
                    mode="json", exclude_none=True
                )
                ctx.editorial.save(uid(location), validated, expected)
                return redirect(
                    request,
                    "locations",
                    "Черновик сохранён. Публикация выполняется отдельно.",
                    location=location,
                )
            if action == "publish":
                ctx.editorial.publish(uid(location), uid(key(form, "version_id")), expected)
                return redirect(request, "locations", "Версия опубликована", location=location)
            if action == "restore":
                ctx.editorial.restore(uid(location), uid(key(form, "version_id")), expected)
                return redirect(
                    request, "locations", "Версия восстановлена в черновик", location=location
                )
            if action == "attach":
                ctx.locations.attach(uid(key(form, "restaurant_id")), uid(location))
                return redirect(
                    request, "locations", "Ресторан связан с локацией", location=location
                )
            if action == "select-restaurant":
                selected = ctx.queries.location(uid(location))
                item = next(
                    (r for r in selected["available"] if r["id"] == key(form, "restaurant_id")),
                    None,
                )
                if item is None:
                    raise RuleViolation("Ресторан недоступен")
                content = selected["content"]
                if any(r.get("id") == item["id"] for r in content["restaurants"]):
                    raise RuleViolation("Ресторан уже в подборке")
                content["restaurants"].append(item)
                ctx.editorial.save(location, content, expected)
                return redirect(
                    request, "locations", "Ресторан добавлен в черновик", location=location
                )
            if action == "prompt":
                ctx.jobs.set_prompt(key(form, "prompt"))
                return redirect(request, "fill", "Промпт сохранён")
            if action == "generation-model":
                ctx.jobs.set_generation_model(key(form, "model"))
                return redirect(
                    request,
                    "fill",
                    "Модель наполнения сохранена. Новые запросы используют её; отзывы не затронуты.",
                )
            if action == "regenerate":
                new_id = ctx.jobs.regenerate(
                    uid(key(form, "job_id")), model=key(form, "model") or None
                )
                return redirect(
                    request,
                    "ai-audit",
                    "Создан новый запрос с текущим промтом и выбранной моделью. Старый журнал сохранён.",
                    job=new_id,
                )
            if action == "generate":
                count = ctx.jobs.enqueue(
                    uid(location) if location else None,
                    key(form, "scope") or "empty",
                    model=key(form, "model") or None,
                )
                return redirect(
                    request, "fill", f"Поставлено в очередь: {count}. Публикация отдельно."
                )
            if action == "retry":
                ctx.jobs.retry(uid(key(form, "job_id")))
                return redirect(request, "fill", "Задание поставлено повторно")
            if action == "cancel":
                ctx.jobs.cancel()
                return redirect(request, "fill", "Ожидающие задания отменены")
            if action == "moderate":
                ctx.editorial.moderate(
                    uid(key(form, "review_id")), key(form, "decision"), key(form, "reason")
                )
                return redirect(
                    request, "reviews", "Решение сохранено", review=key(form, "review_id")
                )
            if action == "proposal":
                ctx.editorial.proposal(
                    uid(key(form, "proposal_id")),
                    None if key(form, "decision") == "reject" else card_from_form(form),
                )
                return redirect(
                    request,
                    "reviews",
                    "Решение сохранено; изменения находятся в черновике",
                    review=key(form, "review_id"),
                )
            if action == "revoke-admin":
                ctx.security.revoke(admin_token(request), all_sessions=True)
                return redirect(request, message="Сессии завершены")
            if action == "revoke-alex":
                ctx.security.revoke("", alex=True, all_sessions=True)
                return RedirectResponse(ctx.settings.path("/alex"), status_code=303)
            raise HTTPException(404, "Неизвестное действие")
        except ValueError as exc:
            message = (
                str(exc) if isinstance(exc, RuleViolation) else "Проверьте поля формы и формат JSON"
            )
            return redirect(
                request,
                "locations" if location else "overview",
                message,
                **({"location": location} if location else {}),
            )

    @router.get(prefix + "/alex")
    def alex(request: Request) -> Response:
        ctx = context(request)
        principal = ctx.security.principal(request.cookies.get("guide_alex"), alex=True)
        admin = ctx.security.principal(admin_token(request))
        changes = ctx.queries.rows("alex_changes") if principal or admin else []
        transcripts = (
            {
                row["id"]: row["transcript"]
                for row in ctx.queries.rows("reviews")
                if row["alex_session_id"]
            }
            if principal or admin
            else {}
        )
        token = admin_token(request) if admin else request.cookies.get("guide_alex", "")
        return render(
            request,
            "alex.html",
            {
                "alex": principal,
                "admin": admin,
                "sessions": ctx.queries.rows("alex_sessions") if principal or admin else [],
                "changes": changes,
                "transcripts": transcripts,
                "csrf_token": ctx.security.csrf(token),
            },
        )

    @router.post(prefix + "/alex/logout")
    def alex_logout(request: Request, form: FormData = Depends(form_data)) -> Response:
        ctx = context(request)
        if not ctx.security.principal(request.cookies.get("guide_alex"), alex=True):
            raise HTTPException(401)
        check_csrf(request, form, alex=True)
        ctx.security.revoke(request.cookies["guide_alex"], alex=True)
        response = RedirectResponse(ctx.settings.path("/"), status_code=303)
        response.delete_cookie("guide_alex", path=prefix)
        return response

    return router
