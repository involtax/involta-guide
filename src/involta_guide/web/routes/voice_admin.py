import json
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from starlette.datastructures import FormData

from involta_guide.domain.entities import RuleViolation
from involta_guide.infrastructure.voice.client import VoiceUnavailable
from involta_guide.infrastructure.voice.editorial import Decision, VoiceEditorial
from involta_guide.infrastructure.voice.read_model import VoiceReadModel
from involta_guide.web.http import check_csrf, context, form_data, require_admin
from involta_guide.web.routes.admin import render
from involta_guide.web.routes.voice import voice_client


def routes(prefix: str) -> APIRouter:
    router = APIRouter()
    base = prefix + "/admin/voice"

    def show(request: Request, review_id: str | None = None, run: int | None = None) -> Response:
        require_admin(request)
        ctx = context(request)
        params = request.query_params
        read = VoiceReadModel(ctx.engine)
        listing = read.listing(
            params.get("q", ""),
            params.get("moderation", ""),
            params.get("kind", ""),
            params.get("repeat") == "1",
            int(params.get("page", "1")),
            params.get("stage", ""),
        )
        detail = read.detail(review_id) if review_id else None
        remote_settings = None
        remote = None
        unavailable = ""
        pending = []
        if ctx.settings.voice_service_secret.get_secret_value():
            try:
                remote_settings = voice_client(request).json("GET", "/admin/settings")
                if detail and detail["link"]:
                    ref = str(detail["link"]["reference"])
                    remote = voice_client(request).json("GET", "/admin/submissions/" + ref)
                    if run is not None:
                        remote = voice_client(request).json(
                            "GET", f"/admin/submissions/{ref}/runs/{run}"
                        )
                if not detail:
                    remote_rows = voice_client(request).json(
                        "GET", "/admin/submissions", params={"page": listing["page"]}
                    )["rows"]
                    local = {str(r["id"]) for r in ctx.queries.rows("reviews", limit=100000)}
                    pending = [r for r in remote_rows if r["reference"] not in local]
            except VoiceUnavailable as exc:
                unavailable = str(exc)
        return render(
            request,
            "voice_admin.html",
            {
                "tab": "voice",
                "queue": listing,
                "v": detail,
                "remote": remote,
                "run_log": run is not None,
                "pending_voice": pending,
                "voice_settings": remote_settings,
                "voice_unavailable": unavailable,
                "message": params.get("message", ""),
                "q": params.get("q", ""),
                "moderation": params.get("moderation", ""),
                "proposal_kind": params.get("kind", ""),
                "repeat_filter": params.get("repeat", "") == "1",
                "stage_filter": params.get("stage", ""),
                "new_key": lambda: str(uuid4()),
                "page_query": lambda p: urlencode({**dict(params), "page": p}),
            },
        )

    @router.get(base)
    def index(request: Request) -> Response:
        return show(request)

    @router.get(base + "/legacy/{review_id}")
    def legacy(request: Request, review_id: UUID) -> Response:
        return show(request, str(review_id))

    @router.get(base + "/submission/{reference}")
    def unimported(request: Request, reference: UUID) -> Response:
        require_admin(request)
        data = voice_client(request).json("GET", "/admin/submissions/" + str(reference))
        return render(request, "voice_pending.html", {"tab": "voice", "pending": data})

    @router.get(base + "/{slug}/{number}")
    def numbered(request: Request, slug: str, number: int) -> Response:
        require_admin(request)
        key, canonical = VoiceReadModel(context(request).engine).resolve(slug, number)
        if slug != canonical:
            return RedirectResponse(base + "/" + canonical + "/" + str(number), status_code=308)
        return show(request, key)

    @router.get(base + "/{slug}/{number}/runs/{run}")
    def run(request: Request, slug: str, number: int, run: int) -> Response:
        require_admin(request)
        key, canonical = VoiceReadModel(context(request).engine).resolve(slug, number)
        if slug != canonical:
            return RedirectResponse(base + f"/{canonical}/{number}/runs/{run}", status_code=308)
        return show(request, key, run)

    @router.get(base + "/media/audio/{reference}")
    def audio(request: Request, reference: UUID) -> Response:
        require_admin(request)
        remote = voice_client(request).request(
            "GET", "/admin/submissions/" + str(reference) + "/audio"
        )
        return Response(
            remote.content,
            status_code=remote.status_code,
            media_type=remote.headers.get("content-type", "application/octet-stream"),
            headers={"Cache-Control": "private,no-store"},
        )

    @router.post(base + "/settings")
    def settings(request: Request, form: FormData = Depends(form_data)) -> Response:
        principal = require_admin(request)
        check_csrf(request, form)
        values = {
            key: str(form.get(key) or "")
            for key in ("transcription_model", "analysis_model", "prompt")
        }
        before = voice_client(request).json("GET", "/admin/settings")
        after = voice_client(request).json(
            "PUT", "/admin/settings", json=values, actor_session=principal["id"]
        )
        from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow

        with PostgresUow(context(request).engine) as uow:
            uow.repo.audit(
                "voice_settings",
                "voice",
                json.dumps(
                    {
                        "actor": "guide-editor",
                        "session_id": principal["id"],
                        "before": before,
                        "after": after,
                    },
                    ensure_ascii=False,
                ),
            )
        return RedirectResponse(base + "?message=" + "Настройки+сохранены", status_code=303)

    @router.post(base + "/decision/{review_id}")
    def decision(
        request: Request, review_id: UUID, form: FormData = Depends(form_data)
    ) -> Response:
        principal = require_admin(request)
        check_csrf(request, form)
        ctx = context(request)
        read = VoiceReadModel(ctx.engine)
        detail = read.detail(str(review_id))
        action = str(form.get("action") or "")
        if action in ("new-run", "revalidate"):
            if not detail["link"]:
                raise HTTPException(409, "Старая запись обрабатывается прежним исполнителем")
            if detail["text"]["revision"] != int(str(form.get("review_revision"))) or detail[
                "link"
            ]["active_run"] != int(str(form.get("processing_run"))):
                raise HTTPException(409, "Отзыв изменился; обновите страницу")
            ref = str(detail["link"]["reference"])
            path = f"/admin/submissions/{ref}/runs"
            body: dict[str, Any] = {"idempotency_key": str(UUID(str(form.get("idempotency_key"))))}
            if action == "revalidate":
                path += f"/{int(str(form.get('run')))}/revalidate"
            if action == "new-run":
                location = detail["location"]
                body["context"] = {
                    "id": str(location["id"]),
                    "slug": location["slug"],
                    "name": location["name"],
                    "revision": str(location["draft_version"])
                    if location["draft_version"]
                    else None,
                    "published_revision": str(location["published_version"])
                    if location["published_version"]
                    else None,
                    "restaurants": [
                        {
                            "id": str(r["id"]),
                            "name": r["name"],
                            "phone": r["phone"],
                            "description": r["description"],
                            "openingHours": r["opening_hours"],
                        }
                        for r in detail["restaurants"]
                    ],
                }
            result = voice_client(request).json(
                "POST", path, json=body, actor_session=principal["id"]
            )
            from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow

            with PostgresUow(ctx.engine) as uow:
                uow.repo.audit(
                    "voice_" + action,
                    str(review_id),
                    json.dumps(
                        {
                            "actor": "guide-editor",
                            "session_id": principal["id"],
                            "key": body["idempotency_key"],
                            "before_run": detail["link"]["active_run"],
                            "result_run": result["run"],
                        }
                    ),
                )
            return RedirectResponse(
                ctx.settings.path(detail["path"]) + "?message=Обработка+запрошена", status_code=303
            )
        changes: dict[str, Any] = {}
        if action == "approve-proposal":
            for name in ("name", "cuisine", "description", "mapsUrl", "phone"):
                if name in form:
                    changes[name] = str(form.get(name) or "").strip()
            if form.get("clear_hours") == "yes":
                changes["openingHours"] = None
            elif form.get("hours_enabled") == "yes":
                proposal = next(
                    (
                        p
                        for p in detail["proposals"]
                        if str(p["id"]) == str(form.get("proposal_id"))
                    ),
                    None,
                )
                existing_hours = (proposal["final"].get("openingHours") or {}) if proposal else {}
                exceptions = dict(existing_hours.get("exceptions", {}))
                for index in range(11):
                    if f"exception_{index}_date" not in form:
                        continue
                    original = str(form.get(f"exception_{index}_original") or "")
                    date = str(form.get(f"exception_{index}_date") or "")
                    removed = form.get(f"exception_{index}_remove") == "yes"
                    if original and (removed or date != original):
                        exceptions.pop(original, None)
                    if date and not removed:
                        pairs = []
                        for slot in range(4):
                            start = str(form.get(f"exception_{index}_{slot}_start") or "")
                            end = str(form.get(f"exception_{index}_{slot}_end") or "")
                            if start or end:
                                pairs.append([start, end])
                        exceptions[date] = pairs
                week = []
                for day in range(7):
                    intervals = []
                    for slot in range(4):
                        start = str(form.get(f"hours_{day}_{slot}_start") or "")
                        end = str(form.get(f"hours_{day}_{slot}_end") or "")
                        if start or end:
                            intervals.append([start, end])
                    week.append(intervals)
                changes["openingHours"] = {
                    "timezone": str(form.get("timezone") or "Europe/Moscow"),
                    "week": week,
                    "exceptions": exceptions,
                }
        command = Decision(
            idempotency_key=UUID(str(form.get("idempotency_key"))),
            review_revision=int(str(form.get("review_revision"))),
            processing_run=int(str(form.get("processing_run"))),
            action=action,
            reason=str(form.get("reason") or ""),
            text=str(form.get("text")) if "text" in form else None,
            restaurant_id=UUID(str(form.get("restaurant_id")))
            if form.get("restaurant_id")
            else None,
            proposal_id=UUID(str(form.get("proposal_id"))) if form.get("proposal_id") else None,
            proposal_revision=int(str(form.get("proposal_revision")))
            if form.get("proposal_revision")
            else None,
            page_revision=UUID(str(form.get("page_revision")))
            if form.get("page_revision")
            else None,
            changes=changes,
            run=int(str(form.get("run"))) if form.get("run") else None,
            fragment_number=int(str(form.get("fragment_number")))
            if form.get("fragment_number") is not None
            else None,
            version_id=UUID(str(form.get("version_id"))) if form.get("version_id") else None,
            published_revision=UUID(str(form.get("published_revision")))
            if form.get("published_revision")
            else None,
        )
        try:
            VoiceEditorial(ctx.engine).decide(str(review_id), command, principal["id"])
        except RuleViolation:
            raise
        return RedirectResponse(
            ctx.settings.path(detail["path"]) + "?message=Решение+сохранено", status_code=303
        )

    return router
