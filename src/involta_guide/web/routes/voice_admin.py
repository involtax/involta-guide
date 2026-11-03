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
    base = prefix + "/admin/reviews"
    old_base = prefix + "/admin/voice"

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
        asr_comparison = None
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
        # Settings availability must not hide already saved recognition results.
        if detail and detail["asr_reference"]:
            try:
                asr_comparison = voice_client(request).json(
                    "GET", "/admin/submissions/" + detail["asr_reference"] + "/asr"
                )
            except VoiceUnavailable as exc:
                unavailable = str(exc)
        return render(
            request,
            "voice_admin.html",
            {
                "tab": "reviews",
                "queue": listing,
                "consumer_health": read.consumer_health(),
                "v": detail,
                "remote": remote,
                "asr_comparison": asr_comparison,
                "asr_action": ctx.settings.path(detail["path"] + "/actions") if detail else "",
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

    @router.get(old_base)
    def old_index(request: Request) -> Response:
        require_admin(request)
        query = "?" + str(request.query_params) if request.query_params else ""
        return RedirectResponse(base + query, status_code=308)

    @router.get(old_base + "/{slug}/{number:int}")
    @router.get(old_base + "/{slug}/{number:int}/runs/{run}")
    def old_numbered(request: Request, slug: str, number: int, run: int | None = None) -> Response:
        require_admin(request)
        path = VoiceReadModel(context(request).engine).imported_path(slug, number)
        if run is not None:
            path += "/runs/" + str(run)
        return RedirectResponse(context(request).settings.path(path), status_code=308)

    @router.get(old_base + "/legacy/{review_id}")
    def legacy(request: Request, review_id: UUID) -> Response:
        require_admin(request)
        path = VoiceReadModel(context(request).engine).path(str(review_id))
        return RedirectResponse(context(request).settings.path(path), status_code=308)

    @router.get(old_base + "/submission/{reference}")
    def old_pending(request: Request, reference: UUID) -> Response:
        require_admin(request)
        return RedirectResponse(base + "/submission/" + str(reference), status_code=308)

    @router.get(base + "/submission/{reference}")
    def unimported(request: Request, reference: UUID) -> Response:
        require_admin(request)
        data = voice_client(request).json("GET", "/admin/submissions/" + str(reference))
        comparison = voice_client(request).json(
            "GET", "/admin/submissions/" + str(reference) + "/asr"
        )
        return render(
            request,
            "voice_pending.html",
            {
                "tab": "reviews",
                "pending": data,
                "asr_comparison": comparison,
                "new_key": lambda: str(uuid4()),
                "asr_action": context(request).settings.path(
                    "/admin/reviews/submission/" + str(reference)
                ),
            },
        )

    def asr_action(request: Request, reference: str, form: FormData, session: Any) -> None:
        action = str(form.get("action") or "")
        if action not in ("asr-run", "asr-evaluate"):
            raise HTTPException(400, "Неизвестное действие сравнения")
        path = "/admin/submissions/" + reference + "/asr-runs"
        payload: dict[str, Any] = {"idempotency_key": str(UUID(str(form.get("idempotency_key"))))}
        if action == "asr-evaluate":
            path += "/" + str(int(str(form.get("asr_run")))) + "/evaluation"
            payload.update(
                revision=int(str(form.get("asr_revision"))),
                candidates_digest=str(form.get("candidates_digest") or ""),
                preferred_engine=str(form.get("preferred_engine") or "") or None,
                reference_text=str(form.get("reference_text") or "").strip() or None,
                note=str(form.get("note") or ""),
            )
        else:
            payload["force"] = form.get("force") == "yes"
        voice_client(request).json("POST", path, json=payload, actor_session=session)

    @router.post(old_base + "/submission/{reference}")
    @router.post(base + "/submission/{reference}")
    def unimported_action(
        request: Request, reference: UUID, form: FormData = Depends(form_data)
    ) -> Response:
        principal = require_admin(request)
        check_csrf(request, form)
        asr_action(request, str(reference), form, principal["id"])
        return RedirectResponse(base + "/submission/" + str(reference), status_code=303)

    @router.get(base + "/{slug}/{number:int}")
    def numbered(request: Request, slug: str, number: int) -> Response:
        require_admin(request)
        key, _ = VoiceReadModel(context(request).engine).resolve(slug, number)
        return show(request, key)

    @router.get(base + "/{slug}/{number:int}/runs/{run}")
    def run(request: Request, slug: str, number: int, run: int) -> Response:
        require_admin(request)
        key, _ = VoiceReadModel(context(request).engine).resolve(slug, number)
        return show(request, key, run)

    @router.get(old_base + "/media/audio/{reference}")
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

    @router.post(old_base + "/settings")
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

    @router.post(base + "/{slug}/{number:int}/actions")
    def numbered_action(
        request: Request, slug: str, number: int, form: FormData = Depends(form_data)
    ) -> Response:
        require_admin(request)
        review_id, _ = VoiceReadModel(context(request).engine).resolve(slug, number)
        return decision(request, UUID(review_id), form)

    @router.get(base + "/{slug}/{number:int}/audio")
    def review_audio(request: Request, slug: str, number: int) -> Response:
        require_admin(request)
        ctx = context(request)
        review_id, _ = VoiceReadModel(ctx.engine).resolve(slug, number)
        review = ctx.queries.get("reviews", review_id)
        if review["audio"].startswith("voice:"):
            remote = voice_client(request).request(
                "GET", "/admin/submissions/" + review_id + "/audio"
            )
            return Response(
                remote.content,
                status_code=remote.status_code,
                media_type=remote.headers.get("content-type", "application/octet-stream"),
                headers={"Cache-Control": "private,no-store"},
            )
        content, mime = ctx.reviews.audio(review_id)
        return Response(content, media_type=mime, headers={"Cache-Control": "private,no-store"})

    @router.post(old_base + "/decision/{review_id}")
    def decision(
        request: Request, review_id: UUID, form: FormData = Depends(form_data)
    ) -> Response:
        principal = require_admin(request)
        check_csrf(request, form)
        ctx = context(request)
        read = VoiceReadModel(ctx.engine)
        detail = read.detail(str(review_id))
        action = str(form.get("action") or "")
        if action in ("asr-run", "asr-evaluate"):
            reference = detail["asr_reference"]
            if not reference:
                from involta_guide.infrastructure.voice.backfill import ensure_copy

                reference = ensure_copy(
                    ctx.settings, ctx.engine, str(review_id), voice_client(request)
                )
            asr_action(request, reference, form, principal["id"])
            return RedirectResponse(ctx.settings.path(detail["path"]), status_code=303)
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
                    "revision": str(location["revision"]) if location["revision"] else None,
                    "published_revision": str(location["revision"])
                    if location["revision"]
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
                if action == "new-run":
                    from sqlalchemy import text

                    uow.session.execute(
                        text("UPDATE review_texts SET revision=revision+1 WHERE review_id=:id"),
                        {"id": str(review_id)},
                    )
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
        if action == "edit-proposal":
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
            affected_revisions=json.loads(str(form.get("affected_revisions") or "{}")),
            run=int(str(form.get("run"))) if form.get("run") else None,
            fragment_number=int(str(form.get("fragment_number")))
            if form.get("fragment_number") is not None
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
