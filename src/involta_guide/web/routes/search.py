"""Search API is independent of pages: there are no public GET search-result pages."""

import logging
from time import perf_counter
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from involta_guide.search.catalogue import catalogue
from involta_guide.search.journal import Journal
from involta_guide.search.matching import VERSION, search
from involta_guide.web.http import (
    admin_token,
    check_origin,
    client_ip,
    context,
    require_diagnostics,
)

logger = logging.getLogger(__name__)
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: UUID
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=100)]


class ShownResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Annotated[
        str, StringConstraints(min_length=3, max_length=180, pattern=r"^(location|restaurant):")
    ]
    kind: Literal["location", "restaurant"]
    name: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    location: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    context: Annotated[str, StringConstraints(max_length=500)]
    anchor: Annotated[str, StringConstraints(max_length=100)] = ""
    match: Annotated[str, StringConstraints(max_length=30)] = "local"


class SearchEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: UUID
    query: Text
    seq: int = Field(ge=1, le=1000000)
    shown: list[ShownResult] | None = Field(default=None, max_length=8)
    version: Annotated[
        str, StringConstraints(min_length=1, max_length=80, pattern=r"^[a-z0-9._-]+$")
    ] = "local-names-v1"
    complete: bool = False
    cache_hit: bool = False
    client_error: bool = False
    reason: Literal["blur", "refined", "clear", "leave", "select"] | None = None
    clicked_id: Annotated[str, StringConstraints(max_length=180)] | None = None

    @model_validator(mode="after")
    def click_was_shown(self) -> "SearchEvent":
        ids = [item.id for item in self.shown or []]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate displayed result")
        if self.clicked_id and (self.reason != "select" or self.clicked_id not in ids):
            raise ValueError("Click must reference a displayed result")
        if self.reason == "select" and not self.clicked_id:
            raise ValueError("Selected result is missing")
        return self


def routes(prefix: str) -> APIRouter:
    router = APIRouter()

    @router.post(prefix + "/api/search")
    def suggestions(request: Request, body: SearchRequest) -> Response:
        check_origin(request)
        ctx = context(request)
        started = perf_counter()
        is_admin = ctx.security.principal(admin_token(request)) is not None
        try:
            results = search(catalogue(ctx.engine), body.query)
        except Exception:
            Journal(ctx.engine).server(
                body.token,
                body.query,
                client_ip(request),
                is_admin,
                [],
                (perf_counter() - started) * 1000,
                error=True,
            )
            logger.exception("Catalogue search failed")
            raise HTTPException(503, "Поиск временно недоступен") from None
        elapsed = (perf_counter() - started) * 1000
        Journal(ctx.engine).server(
            body.token, body.query, client_ip(request), is_admin, results, elapsed
        )
        return JSONResponse(
            dict(results=results, version=VERSION),
            headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"},
        )

    @router.post(prefix + "/api/search/events", status_code=204)
    def event(request: Request, body: SearchEvent) -> Response:
        check_origin(request)
        ctx = context(request)
        is_admin = ctx.security.principal(admin_token(request)) is not None
        Journal(ctx.engine).event(
            body.token, body.query, client_ip(request), is_admin, body.model_dump(mode="json")
        )
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    @router.get(prefix + "/logs/search/replay")
    def replay(request: Request, text: Text) -> Response:
        require_diagnostics(request)
        results = search(catalogue(context(request).engine), text)
        return JSONResponse(
            dict(query=text, results=results, version=VERSION),
            headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"},
        )

    @router.get(prefix + "/logs/search")
    @router.get(prefix + "/logs/search.json")
    def logs(
        request: Request,
        before: Annotated[int | None, Query(gt=0)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        outcome: Literal[
            "all", "empty", "no_click", "error", "selected", "refined", "pending", "not_shown"
        ] = "all",
        include_admin: bool = False,
    ) -> Response:
        require_diagnostics(request)
        ctx = context(request)
        report = Journal(ctx.engine).report(
            before=before, limit=limit, outcome=outcome, include_admin=include_admin
        )
        headers = {"Cache-Control": "private, no-store", "X-Robots-Tag": "noindex"}
        if request.url.path.endswith(".json"):
            return JSONResponse(jsonable_encoder(report), headers=headers)
        return request.app.state.templates.TemplateResponse(
            request=request,
            name="search_logs.html",
            headers=headers,
            context=dict(
                settings=ctx.settings,
                report=report,
                title="Журнал поиска — Инвольта гид",
                outcome=outcome,
                include_admin=include_admin,
                limit=limit,
            ),
        )

    return router
