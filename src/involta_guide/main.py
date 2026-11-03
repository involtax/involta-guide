from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import SQLAlchemyError

from involta_guide.application.services.editorial import EditorialService
from involta_guide.application.services.jobs import JobService
from involta_guide.application.services.locations import LocationService, Queries
from involta_guide.application.services.reviews import ReviewService
from involta_guide.config import Settings
from involta_guide.domain.entities import Conflict, NotFound, RuleViolation
from involta_guide.infrastructure.ai.openai import OpenAIProvider
from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow, create_database
from involta_guide.infrastructure.security import ADMIN_SESSION_MAX_AGE, Security
from involta_guide.infrastructure.storage.audio import FileAudioStorage
from involta_guide.web.context import Context
from involta_guide.web.routes import admin, api, public, voice, voice_admin


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = create_database(settings)
    storage = FileAudioStorage(settings.data_dir, settings.max_audio_bytes)
    provider = OpenAIProvider(settings, engine, storage)

    def factory() -> PostgresUow:
        return PostgresUow(engine)

    ctx = Context(
        settings,
        engine,
        Queries(factory),
        EditorialService(factory),
        LocationService(factory),
        ReviewService(factory, storage, settings.pending_review_limit),
        JobService(
            factory, provider, storage, public_fill_enabled=settings.temporary_public_fill_enabled
        ),
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

    directory = Path(__file__).parent / "web"
    app.state.templates = Jinja2Templates(directory=directory / "templates")
    app.state.templates.env.policies["json.dumps_kwargs"]["ensure_ascii"] = False
    app.mount(settings.path("/assets"), StaticFiles(directory=directory / "static"), name="assets")

    @app.get(settings.path("/widget.js"))
    def widget() -> object:
        from fastapi.responses import FileResponse

        return FileResponse(
            directory / "static/widget.js",
            media_type="text/javascript",
            headers={"Cache-Control": "public,max-age=300"},
        )

    @app.get(settings.path("/health/live"))
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(settings.path("/health/ready"))
    def ready() -> dict[str, str]:
        ctx.queries.health()
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
    app.include_router(api.routes(settings.base_path))
    app.include_router(public.routes(settings.base_path))
    app.include_router(voice.routes(settings.base_path))
    app.include_router(voice_admin.routes(settings.base_path))
    return app
