import logging
from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

from fastapi import Request
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from involta_guide.web.http import admin_token, context
from involta_stats import add_hit

logger = logging.getLogger(__name__)


class AudienceStats(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        if not response.headers.get("content-type", "").startswith("text/html"):
            return response
        # The footer and visitor identity belong to this browser, never a shared cache.
        response.headers["Cache-Control"] = "private, no-store"
        response.headers.append("Vary", "Cookie")
        if (
            request.method != "GET"
            or response.status_code != 200
            or not getattr(request.state, "audience_page", False)
        ):
            return response

        ctx = context(request)
        try:
            if await run_in_threadpool(ctx.security.principal, admin_token(request)):
                return response
            try:
                visitor = UUID(request.cookies.get("guide_visitor", ""))
                new_visitor = False
            except ValueError:
                visitor, new_visitor = uuid4(), True
            if new_visitor:
                response.set_cookie(
                    "guide_visitor",
                    str(visitor),
                    max_age=365 * 86400,
                    path=ctx.settings.base_path,
                    httponly=True,
                    secure=ctx.settings.cookie_secure,
                    samesite="lax",
                )
            await run_in_threadpool(add_hit, ctx.engine, visitor)
        except SQLAlchemyError as exc:
            # Optional analytics must not prevent serving a public page.
            logger.warning("Audience hit was not recorded (%s)", type(exc).__name__)
        return response
