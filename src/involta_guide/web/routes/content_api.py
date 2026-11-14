"""Private machine API. No cookie authentication, no implicit model calls on reads/imports."""

import hmac
import json
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from involta_guide.catalogue.schemas import Operation, Scope
from involta_guide.catalogue.service import CityContent, card_complete
from involta_guide.infrastructure.ai.audit import AIAudit
from involta_guide.infrastructure.ai.background import resume_generation
from involta_guide.web.http import context


class ImportResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    expected: str = Field(pattern=r"^[a-f0-9]{64}$")
    result: dict[str, Any]


class Run(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID


class Reprocess(Run):
    expected_revision: UUID | None


def authenticate(request: Request) -> None:
    secret = context(request).settings.agent_api_token.get_secret_value()
    authorization = request.headers.get("authorization", "")
    if len(secret) < 32 or not hmac.compare_digest(
        authorization.encode(), ("Bearer " + secret).encode()
    ):
        raise HTTPException(401, "Требуется Bearer-токен административного API")


async def bounded_body(request: Request) -> dict[str, Any]:
    authenticate(request)
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 1024 * 1024:
            raise HTTPException(413, "Ответ слишком большой")
        chunks.append(chunk)
    try:
        value = json.loads(b"".join(chunks))
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Нужен JSON") from None
    if not isinstance(value, dict):
        raise HTTPException(422, "Нужен объект JSON")
    return value


def service(request: Request) -> CityContent:
    ctx = context(request)
    return CityContent(ctx.jobs.uow, ctx.settings.openai_model)


def routes(prefix: str) -> APIRouter:
    router = APIRouter(prefix=prefix + "/api/admin", dependencies=[Depends(authenticate)])

    @router.get("/locations/{location_id}/{operation}/context")
    def prepare(
        request: Request,
        location_id: int,
        operation: Operation,
        restaurant_id: UUID | None = None,
        scope: Scope = "all",
    ) -> JSONResponse:
        result = service(request).prepare(
            location_id, operation, str(restaurant_id) if restaurant_id else None, scope
        )
        return JSONResponse(
            result, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"}
        )

    @router.post("/locations/{location_id}/{operation}/result")
    def submit(
        request: Request,
        location_id: int,
        operation: Operation,
        restaurant_id: UUID | None = None,
        scope: Scope = "all",
        body: dict[str, Any] = Depends(bounded_body),
    ) -> dict[str, Any]:
        value = ImportResult.model_validate(body)
        return service(request).submit(
            location_id,
            operation,
            str(value.request_id),
            value.expected,
            value.result,
            str(restaurant_id) if restaurant_id else None,
            scope,
        )

    @router.post("/locations/{location_id}/{operation}/run", status_code=202)
    def run(
        request: Request,
        location_id: int,
        operation: Operation,
        restaurant_id: UUID | None = None,
        scope: Scope = "all",
        body: dict[str, Any] = Depends(bounded_body),
    ) -> dict[str, Any]:
        value = Run.model_validate(body)
        identity = service(request).enqueue(
            location_id,
            operation,
            str(restaurant_id) if restaurant_id else None,
            scope,
            str(value.request_id),
        )
        return {"job_id": identity}

    @router.get("/locations/{location_id}")
    def catalogue(request: Request, location_id: int) -> dict[str, Any]:
        from involta_guide.application.services.editorial import require

        ctx = context(request)
        with ctx.jobs.uow() as work:
            location = require(work.repo, "locations", location_id)
            return {
                "id": location_id,
                "name": location["name"],
                "intro": location["intro"],
                "restaurants": [
                    {
                        "card_complete": card_complete(row),
                        **{
                            key: row[key]
                            for key in (
                                "id",
                                "number",
                                "name",
                                "active",
                                "hidden",
                                "verification_status",
                                "maps_url",
                                "ranking",
                            )
                        },
                    }
                    for row in work.repo.rows(
                        "restaurants", {"location_id": location_id}, limit=10000
                    )
                ],
            }

    @router.get("/jobs/{job_id}")
    def job(request: Request, job_id: UUID) -> dict[str, Any]:
        from involta_guide.application.services.editorial import require

        with context(request).jobs.uow() as work:
            row = require(work.repo, "jobs", str(job_id))
            if not (row["report"] or {}).get("operation"):
                raise HTTPException(404)
            location = require(work.repo, "locations", row["location_id"])
            return {
                **{key: row[key] for key in ("id", "status", "applied", "error", "report")},
                "location_id": row["location_id"],
                "location_revision": location["revision"],
            }

    @router.post("/jobs/{job_id}/reprocess", status_code=202)
    def reprocess(
        request: Request,
        job_id: UUID,
        body: dict[str, Any] = Depends(bounded_body),
    ) -> dict[str, Any]:
        value = Reprocess.model_validate(body)
        job(request, job_id)  # Same content-only scope and 404 as the status endpoint.
        ctx = context(request)
        queued = resume_generation(
            ctx.engine,
            str(job_id),
            ctx.settings.generation_wait_seconds,
            reprocess=True,
            expected_version=str(value.expected_revision) if value.expected_revision else None,
            request_id=str(value.request_id),
        )
        if queued:
            AIAudit(ctx.engine, ctx.settings).record(
                str(job_id),
                "generation_reprocessed",
                {"new_generation": False, "source": "api", "request_id": str(value.request_id)},
            )
        return {"job_id": str(job_id)}

    @router.post("/restaurants/{restaurant_id}/visibility/{visibility}")
    def visibility(
        request: Request, restaurant_id: UUID, visibility: Literal["show", "hide"]
    ) -> dict[str, bool]:
        service(request).visibility(str(restaurant_id), visibility == "hide")
        return {"hidden": visibility == "hide"}

    return router
