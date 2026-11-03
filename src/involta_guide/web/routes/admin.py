import json
import secrets
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from starlette.datastructures import FormData

from involta_guide.application.commands.schemas import RestaurantInput
from involta_guide.domain.entities import RuleViolation
from involta_guide.domain.rules import FACTOR_LABELS, FACTORS, KINDS
from involta_guide.infrastructure.ai.audit import AIAudit
from involta_guide.infrastructure.ai.catalog import (
    GENERATION_MODEL_IDS,
    GENERATION_MODELS,
    MODELS_URL,
    PRICES_CHECKED,
    PRICING_URL,
)
from involta_guide.infrastructure.security import ADMIN_SESSION_MAX_AGE
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
            "fragment": request.headers.get("HX-Request") == "true"
            and request.headers.get("HX-History-Restore-Request") != "true",
            "admin_ui": True,
            "csrf_token": ctx.security.csrf(token),
            "kinds": KINDS,
            "factors": FACTORS,
            "factor_labels": FACTOR_LABELS,
            "generation_model": ctx.jobs.generation_model(),
            "generation_models": GENERATION_MODELS,
            "generation_model_ids": GENERATION_MODEL_IDS,
            "model_prices_checked": PRICES_CHECKED,
            "model_pricing_url": PRICING_URL,
            "model_catalog_url": MODELS_URL,
            **data,
        },
        headers={"Cache-Control": "private, no-store", "Vary": "HX-Request"},
    )


def redirect(request: Request, tab: str = "overview", message: str = "", **params: str) -> Response:
    if tab == "overview":
        return RedirectResponse(
            context(request).settings.path("/admin") + "?" + urlencode({"message": message}),
            status_code=303,
        )
    return RedirectResponse(
        context(request).settings.path("/admin")
        + "?"
        + urlencode({"tab": tab, "message": message, **params}),
        status_code=303,
    )


def log_redirect(request: Request, path: str, *, code: int = 303) -> Response:
    url = context(request).settings.path(path)
    if request.headers.get("HX-Request") == "true":
        return Response(headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=code)


def submitted_log(request: Request, form: FormData) -> str:
    try:
        number = int(key(form, "log_number"))
    except ValueError:
        raise RuleViolation("Выберите лог города и номер запроса") from None
    job_id = AIAudit(context(request).engine, context(request).settings).resolve(
        key(form, "log_slug"), number
    )
    if not job_id:
        raise HTTPException(404, "Лог не найден")
    return job_id


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
        if tab not in ("overview", "reviews", "ratings", "contacts") or (
            tab == "overview" and ("tab" in params or "location" in params or "city" in params)
        ):
            raise HTTPException(404, "Раздел перенесён на отдельный адрес")
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
        if tab == "reviews":
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
        elif tab == "ratings":
            from involta_guide.domain.rules import aggregate_scores

            data["ratings"] = aggregate_scores(ctx.queries.rows("reviews", limit=10000))
        elif tab == "contacts":
            data["contacts"] = ctx.queries.contacts()
        return render(request, "admin.html", data)

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
            max_age=ADMIN_SESSION_MAX_AGE,
            path=prefix,
            httponly=True,
            secure=ctx.settings.cookie_secure,
            samesite="lax",
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

    @router.get(prefix + "/admin/contacts")
    def contacts(request: Request) -> Response:
        require_admin(request)
        return redirect(request, "contacts")

    @router.post(prefix + "/admin/actions/{action}")
    def action(request: Request, action: str, form: FormData = Depends(form_data)) -> Response:
        require_admin(request)
        check_csrf(request, form)
        ctx = context(request)
        try:
            if action == "retry":
                job_id = submitted_log(request, form)
                ctx.jobs.retry(job_id)
                detail = AIAudit(ctx.engine, ctx.settings).detail(job_id)
                assert detail is not None
                return log_redirect(request, detail["job"]["log_path"])
            if action == "regenerate":
                new_id = ctx.jobs.regenerate(
                    submitted_log(request, form), model=key(form, "model") or None
                )
                detail = AIAudit(ctx.engine, ctx.settings).detail(new_id)
                assert detail is not None
                return log_redirect(request, detail["job"]["log_path"])
            if action in ("resume-generation", "reprocess-generation"):
                from involta_guide.infrastructure.ai.background import resume_generation

                job_id = submitted_log(request, form)
                reprocess = action == "reprocess-generation"
                resume_generation(
                    ctx.engine,
                    job_id,
                    ctx.settings.generation_wait_seconds,
                    reprocess=reprocess,
                    expected_version=uid(key(form, "expected")) if key(form, "expected") else None,
                )
                store = AIAudit(ctx.engine, ctx.settings)
                store.record(
                    job_id,
                    "generation_reprocessed" if reprocess else "generation_resumed",
                    {"new_generation": False},
                )
                detail = store.detail(job_id)
                assert detail is not None
                return log_redirect(request, detail["job"]["log_path"])
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
                    "Решение сохранено; принятые изменения применены на сайте",
                    review=key(form, "review_id"),
                )
            if action == "revoke-admin":
                ctx.security.revoke(admin_token(request), all_sessions=True)
                return redirect(request, message="Сессии завершены")
            if action == "revoke-alex":
                ctx.security.revoke("", alex=True, all_sessions=True)
                ctx.security.revoke(admin_token(request), all_sessions=True)
                return RedirectResponse(ctx.settings.path("/alex"), status_code=303)
            raise HTTPException(404, "Неизвестное действие")
        except ValueError as exc:
            message = (
                str(exc) if isinstance(exc, RuleViolation) else "Проверьте поля формы и формат JSON"
            )
            if (
                action in ("resume-generation", "reprocess-generation", "regenerate", "retry")
                and key(form, "log_number").isdigit()
            ):
                store = AIAudit(ctx.engine, ctx.settings)
                reference = store.resolve(key(form, "log_slug"), int(key(form, "log_number")))
                detail = store.detail(reference) if reference else None
                if detail:
                    return log_redirect(
                        request, detail["job"]["log_path"] + "?" + urlencode({"message": message})
                    )
            return redirect(request, message=message)

    @router.get(prefix + "/alex")
    def alex(request: Request) -> Response:
        ctx = context(request)
        principal = ctx.security.principal(request.cookies.get("guide_alex"), alex=True)
        admin = ctx.security.principal(admin_token(request))
        changes = ctx.queries.rows("alex_changes") if principal or admin else []
        for change in changes:
            change["changes"] = (
                ctx.queries.get("reviews", change["review_id"])["changes"]
                if change["review_id"]
                else []
            )
        transcripts = (
            {
                row["id"]: row["transcript"]
                for row in ctx.queries.rows("reviews")
                if row["alex_session_id"] or row.get("admin_session_id")
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
        admin = ctx.security.principal(admin_token(request))
        if not admin and not ctx.security.principal(request.cookies.get("guide_alex"), alex=True):
            raise HTTPException(401)
        check_csrf(request, form, alex=not bool(admin))
        if admin:
            ctx.security.revoke(admin_token(request))
        else:
            ctx.security.revoke(request.cookies["guide_alex"], alex=True)
        response = RedirectResponse(ctx.settings.path("/"), status_code=303)
        response.delete_cookie("guide_alex", path=prefix)
        response.delete_cookie("guide_admin", path=prefix)
        return response

    return router
