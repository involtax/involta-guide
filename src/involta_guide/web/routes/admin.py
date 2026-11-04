import secrets
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from starlette.datastructures import FormData

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
from involta_guide.infrastructure.voice.read_model import VoiceReadModel
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
            "review_path": lambda key: ctx.settings.path(VoiceReadModel(ctx.engine).path(str(key))),
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


def overview_redirect(request: Request, message: str = "") -> Response:
    path = "/admin" + ("?" + urlencode({"message": message}) if message else "")
    return log_redirect(request, path)


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
        return render(
            request,
            "admin.html",
            {
                "active_section": "overview",
                "message": request.query_params.get("message", ""),
                "overview": ctx.queries.overview(),
            },
        )

    @router.get(prefix + "/admin/ratings")
    def ratings(request: Request) -> Response:
        require_admin(request)
        from involta_guide.domain.rules import aggregate_scores

        return render(
            request,
            "admin.html",
            {
                "active_section": "ratings",
                "ratings": aggregate_scores(context(request).queries.rows("reviews", limit=10000)),
            },
        )

    @router.get(prefix + "/admin/contacts")
    def contacts(request: Request) -> Response:
        require_admin(request)
        return render(
            request,
            "admin.html",
            {"active_section": "contacts", "contacts": context(request).queries.contacts()},
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
            return overview_redirect(request, message=str(exc))
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
        response = overview_redirect(request)
        response.delete_cookie("guide_admin", path=prefix)
        return response

    @router.post(prefix + "/admin/actions/{action}")
    def action(request: Request, action: str, form: FormData = Depends(form_data)) -> Response:
        require_admin(request)
        check_csrf(request, form)
        ctx = context(request)
        try:
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
            if action == "revoke-admin":
                ctx.security.revoke(admin_token(request), all_sessions=True)
                return overview_redirect(request, message="Сессии завершены")
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
            return overview_redirect(request, message=message)

    return router
