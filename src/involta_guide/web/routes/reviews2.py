import json
from datetime import timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import text
from starlette.datastructures import FormData

from involta_guide.infrastructure.ai.audit import AIAudit
from involta_guide.infrastructure.voice.read_model import VoiceReadModel
from involta_guide.review_analysis.service import ReviewAnalysis
from involta_guide.web.http import (
    check_csrf,
    context,
    form_data,
    require_admin,
    require_diagnostics,
)
from involta_guide.web.routes.admin import render
from involta_voice.db import row

LABELS = {"new": "Новый", "ok": "ОК", "bugs": "Баги", "other": "Разное"}
BUCKET = """CASE
 WHEN a.manual_category='bugs' THEN 'bugs'
 WHEN a.source_text IS DISTINCT FROM r.transcript OR a.result IS NULL OR j.status IS DISTINCT FROM 'done' THEN 'new'
 WHEN a.result->>'category' IN ('bugs','other') THEN a.result->>'category'
 WHEN a.approved_at IS NULL THEN 'new'
 ELSE a.result->>'category' END"""
BASE_QUERY = "FROM reviews r JOIN locations l ON l.id=r.location_id LEFT JOIN review_analyses a ON a.review_id=r.id LEFT JOIN jobs j ON j.id=a.job_id"
LIVE = "r.status='transcribed' AND r.content_epoch=l.content_epoch AND NOT l.reset_pending"


def routes(prefix: str) -> APIRouter:
    router = APIRouter()
    base = prefix + "/admin/reviews-2"

    def show(request: Request, status: str, page: int) -> Response:
        require_admin(request)
        if status not in LABELS:
            raise HTTPException(404, "Статус не найден")
        ctx = context(request)
        with ctx.engine.connect() as conn:
            counts: dict[str, int] = dict(
                conn.execute(
                    text(
                        f"SELECT {BUCKET} category,count(*) n {BASE_QUERY} WHERE {LIVE} GROUP BY 1"
                    )
                ).all()
            )
            pages = max(1, (counts.get(status, 0) + 19) // 20)
            page = max(1, min(page, pages))
            rows = [
                dict(r)
                for r in conn.execute(
                    text(f"""SELECT r.id,r.number,r.transcript,r.ip,r.user_agent,r.created_at,
              l.name location_name,l.slug,a.result,a.source_text,a.job_id,a.manual_category,j.status job_status,j.error
              {BASE_QUERY} WHERE {LIVE} AND ({BUCKET})=:status
              ORDER BY r.created_at DESC,r.number DESC LIMIT 20 OFFSET :offset"""),
                    {"status": status, "offset": (page - 1) * 20},
                ).mappings()
            ]
        for item in rows:
            item["date"] = (
                item["created_at"]
                .astimezone(timezone(timedelta(hours=3)))
                .strftime("%d.%m.%Y %H:%M")
            )
            item["ready"] = bool(
                item["result"]
                and item["source_text"] == item["transcript"]
                and item["job_status"] == "done"
            )
        return render(
            request,
            "reviews2.html",
            {
                "active_section": "reviews2",
                "rows": rows,
                "labels": LABELS,
                "counts": counts,
                "selected": status,
                "page": page,
                "pages": pages,
                "processing": any(
                    not r["manual_category"] and r["job_status"] in (None, "queued", "running")
                    for r in rows
                ),
            },
        )

    @router.get(base)
    def index(request: Request, page: int = 1) -> Response:
        return show(request, "new", page)

    @router.get(base + "/{status}")
    def category(request: Request, status: str, page: int = 1) -> Response:
        return show(request, status, page)

    @router.post(base + "/{slug}/{number:int}/{action}")
    def decide(
        request: Request, slug: str, number: int, action: str, form: FormData = Depends(form_data)
    ) -> Response:
        principal = require_admin(request)
        check_csrf(request, form)
        ctx = context(request)
        identity, _ = VoiceReadModel(ctx.engine).resolve(slug, number)
        service = ReviewAnalysis(ctx.engine, ctx.settings)
        if action == "approve":
            service.approve(identity, str(form.get("job_id", "")), principal["id"])
        elif action == "bug":
            service.mark_bug(identity, principal["id"])
        elif action == "delete":
            service.delete(identity, principal["id"])
        else:
            raise HTTPException(404, "Действие не найдено")
        status = str(form.get("status", "new"))
        if status not in LABELS:
            status = "new"
        return RedirectResponse(base + "/" + status, status_code=303)

    @router.get(prefix + "/logs/reviews-2.json")
    def log_index(request: Request, page: int = 1) -> Response:
        require_diagnostics(request)
        ctx = context(request)
        with ctx.engine.connect() as conn:
            entries = [
                dict(r)
                for r in conn.execute(
                    text(f"""SELECT l.slug,r.number,r.created_at,a.result->>'category' category
                {BASE_QUERY} WHERE {LIVE} AND a.job_id IS NOT NULL ORDER BY r.created_at DESC,r.number DESC
                LIMIT 100 OFFSET :offset"""),
                    {"offset": (max(1, page) - 1) * 100},
                ).mappings()
            ]
        for entry in entries:
            entry["url"] = ctx.settings.path(f"/logs/reviews-2/{entry['slug']}/{entry['number']}")
        return JSONResponse(
            jsonable_encoder({"reviews": entries}),
            headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"},
        )

    @router.get(prefix + "/logs/reviews-2/{slug}/{number:int}")
    def log(request: Request, slug: str, number: int) -> Response:
        require_diagnostics(request)
        ctx = context(request)
        identity, _ = VoiceReadModel(ctx.engine).resolve(slug, number)
        with ctx.engine.connect() as conn:
            review = row(conn, "SELECT status FROM reviews WHERE id=:id", id=identity)
            if not review or review["status"] == "deleted":
                raise HTTPException(404, "Отзыв удалён")
            jobs: list[dict[str, Any]] = []
            for job in conn.execute(
                text(
                    "SELECT id,model,prompt,report,status,error,created_at,finished_at FROM jobs WHERE review_id=:id ORDER BY created_at,id"
                ),
                {"id": identity},
            ).mappings():
                events = [
                    json.loads(r["detail"])
                    for r in conn.execute(
                        text(
                            "SELECT detail FROM audit WHERE action='ai_trace' AND target=:id ORDER BY created_at,id"
                        ),
                        {"id": str(job["id"])},
                    ).mappings()
                ]
                jobs.append({**job, "events": events})
            decisions = [
                dict(r)
                for r in conn.execute(
                    text(
                        "SELECT action,detail,created_at FROM audit WHERE target=:id AND action IN ('review_analysis_approved','review_deleted','review_marked_bug') ORDER BY created_at"
                    ),
                    {"id": identity},
                ).mappings()
            ]
        data = AIAudit(ctx.engine, ctx.settings).clean(
            jsonable_encoder(
                {"location": slug, "number": number, "jobs": jobs, "decisions": decisions}
            )
        )
        return JSONResponse(data, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})

    return router
