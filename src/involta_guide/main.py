from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import SQLAlchemyError

import involta_voice
from involta_guide.application.services.jobs import JobService
from involta_guide.application.services.locations import LocationService, Queries
from involta_guide.config import Settings
from involta_guide.domain.entities import Conflict, NotFound, RuleViolation
from involta_guide.infrastructure.ai.openai import OpenAIProvider
from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow, create_database
from involta_guide.infrastructure.security import ADMIN_SESSION_MAX_AGE, Security
from involta_guide.web.context import Context
from involta_guide.web.middleware.audience import AudienceStats
from involta_guide.web.routes import admin, admin_content, public, search, voice, voice_admin


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = create_database(settings)
    provider = OpenAIProvider(settings, engine)

    def factory() -> PostgresUow:
        return PostgresUow(engine)

    ctx = Context(
        settings,
        engine,
        Queries(factory),
        LocationService(factory),
        JobService(factory, provider, public_fill_enabled=settings.temporary_public_fill_enabled),
        Security(settings, factory),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        provider.close()
        engine.dispose()

    app = FastAPI(
        lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False
    )
    app.state.context = ctx
    app.add_middleware(AudienceStats)

    @app.middleware("http")
    async def renew_admin_session(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        from starlette.concurrency import run_in_threadpool

        response = await call_next(request)
        token = request.cookies.get("guide_admin", "")
        if (
            token
            and not request.url.path.startswith(settings.path("/assets/"))
            and request.url.path
            not in (
                settings.path("/admin/login"),
                settings.path("/admin/logout"),
            )
            and response.status_code < 400
            and await run_in_threadpool(ctx.security.renew_admin, token)
        ):
            response.set_cookie(
                "guide_admin",
                token,
                max_age=ADMIN_SESSION_MAX_AGE,
                path=settings.base_path,
                httponly=True,
                secure=settings.cookie_secure,
                samesite="lax",
            )
            response.headers["Cache-Control"] = "private, no-store"
        return response

    from involta_guide.infrastructure.voice.upload_debug import UploadDebug
    from involta_guide.web.middleware.voice_upload import VoiceUploadTrace

    app.add_middleware(
        VoiceUploadTrace,
        store=UploadDebug(engine, settings),
        security=ctx.security,
        prefix=settings.base_path,
        enabled=settings.voice_debug_enabled,
    )

    directory = Path(__file__).parent / "web"

    def public_header_context(request: Request) -> dict[str, bool]:
        admin_path = settings.path("/admin")
        if request.url.path == admin_path or request.url.path.startswith(admin_path + "/"):
            return {"show_admin_link": False}
        token = request.cookies.get("guide_admin")
        return {"show_admin_link": ctx.security.principal(token) is not None}

    app.state.templates = Jinja2Templates(
        directory=directory / "templates", context_processors=[public_header_context]
    )
    app.state.templates.env.policies["json.dumps_kwargs"]["ensure_ascii"] = False
    app.mount(settings.path("/assets"), StaticFiles(directory=directory / "static"), name="assets")
    assert involta_voice.__file__ is not None
    voice_assets = Path(involta_voice.__file__).parent / "static"
    app.mount(
        settings.path("/voice/vad"), StaticFiles(directory=voice_assets / "vad"), name="voice-vad"
    )

    @app.get(settings.path("/widget.js"))
    def widget() -> object:
        from fastapi.responses import FileResponse

        return FileResponse(
            voice_assets / "widget.v1.js",
            media_type="text/javascript",
            headers={"Cache-Control": "public,max-age=300"},
        )

    @app.get(settings.path("/health/live"))
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(settings.path("/health/ready"))
    def ready() -> dict[str, str]:
        try:
            ctx.queries.health()
        except RuleViolation as exc:
            raise HTTPException(503, str(exc)) from exc
        return {"status": "ready", "revision": settings.release_sha}

    @app.exception_handler(RuleViolation)
    def business_error(request: Request, exc: RuleViolation) -> JSONResponse:
        return JSONResponse(
            {"error": str(exc)},
            status_code=404
            if isinstance(exc, NotFound)
            else 409
            if isinstance(exc, Conflict)
            else 400,
        )

    @app.exception_handler(ValueError)
    def validation_error(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"error": "Некорректные данные запроса"}, status_code=400)

    @app.exception_handler(SQLAlchemyError)
    def database_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        return JSONResponse(
            {"error": "Не удалось сохранить данные. Проверьте связи и обновите страницу."},
            status_code=409,
        )

    app.include_router(admin.routes(settings.base_path))
    app.include_router(admin_content.routes(settings.base_path))
    # Specific review diagnostics must precede generic /logs/{slug} routes.
    app.include_router(voice_admin.routes(settings.base_path))
    app.include_router(search.routes(settings.base_path))
    app.include_router(public.routes(settings.base_path))
    app.include_router(voice.routes(settings.base_path))
    return app
