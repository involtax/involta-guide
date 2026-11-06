"""Public integration documentation, discoverable without an MCP client."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from involta_guide.mcp import SERVER_VERSION
from involta_guide.web.http import context


def routes(prefix: str) -> APIRouter:
    router = APIRouter()

    @router.get(prefix + "/for-agents")
    def page(request: Request) -> Response:
        settings = context(request).settings
        return request.app.state.templates.TemplateResponse(
            request=request,
            name="agents.html",
            context={
                "settings": settings,
                "title": "Гид для ИИ-ассистентов — Инвольта гид MCP",
                "description": "Публичный MCP Инвольта гида: подборки ресторанов и отзывы. Два инструмента — Location и Restaurant. Подключение без регистрации.",
                "canonical": settings.url("/for-agents"),
                "indexable": True,
            },
        )

    @router.get(prefix + "/llms.txt")
    @router.get(prefix + "/for-agents.md")
    def instructions(request: Request) -> Response:
        settings = context(request).settings
        body = request.app.state.templates.get_template("agents.md").render(settings=settings)
        return PlainTextResponse(body, headers={"Cache-Control": "public, max-age=300"})

    @router.get(prefix + "/mcp-server.json")
    def manifest(request: Request) -> Response:
        settings = context(request).settings
        return JSONResponse(
            {
                "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
                "name": "io.github.involtax/involta-guide",
                "description": "Curated restaurant selections and public reviews. Location and Restaurant; read-only, no auth.",
                "version": SERVER_VERSION,
                "repository": {
                    "url": "https://github.com/involtax/involta-guide",
                    "source": "github",
                },
                "websiteUrl": settings.url("/for-agents"),
                "remotes": [{"type": "streamable-http", "url": settings.url("/mcp")}],
            },
            headers={"Cache-Control": "public, max-age=300"},
        )

    return router
