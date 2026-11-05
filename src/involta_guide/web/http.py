import hmac
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.datastructures import FormData

from .context import Context


def context(request: Request) -> Context:
    return request.app.state.context


def client_ip(request: Request) -> str:
    ctx = context(request)
    actual = request.client.host if request.client else "unknown"
    if actual in ctx.settings.trusted_proxy_ips.split(","):
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return actual


def check_origin(request: Request) -> None:
    site = urlsplit(context(request).settings.site_url)
    expected = f"{site.scheme}://{site.netloc}"
    if request.headers.get("origin") != expected:
        raise HTTPException(403, "Недопустимый источник запроса")


def admin_token(request: Request) -> str:
    return request.cookies.get("guide_admin", "")


def require_diagnostics(request: Request) -> bool:
    ctx = context(request)
    admin = ctx.security.principal(admin_token(request)) is not None
    if not ctx.settings.public_fill_logs_enabled and not admin:
        raise HTTPException(404, "Публичная диагностика отключена")
    return admin


def require_admin(request: Request) -> dict[str, Any]:
    principal = context(request).security.principal(admin_token(request))
    if principal is None:
        raise HTTPException(401, "Требуется вход")
    return principal


def check_csrf(request: Request, form: FormData, *, login: bool = False) -> None:
    check_origin(request)
    ctx = context(request)
    token = (
        request.cookies.get("guide_login_csrf", "")
        if login
        else request.cookies.get("guide_admin", "")
    )
    expected = ctx.security.csrf(token)
    if not token or not hmac.compare_digest(str(form.get("_csrf", "")), expected):
        raise HTTPException(403, "Неверный CSRF-токен")


async def form_data(request: Request) -> FormData:
    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > 1024 * 1024:
            raise HTTPException(413, "Форма слишком большая")
        chunks.append(chunk)
    body = b"".join(chunks)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    bounded = Request(request.scope, receive)
    return await bounded.form(max_fields=2000, max_part_size=1024 * 1024)
