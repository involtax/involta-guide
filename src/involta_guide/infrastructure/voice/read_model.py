from typing import Any

from sqlalchemy import Engine, text

from involta_guide.domain.entities import NotFound
from involta_voice.db import row


class VoiceReadModel:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def consumer_health(self) -> dict[str, Any]:
        import time

        with self.engine.connect() as conn:
            result = row(conn, "SELECT state FROM voice_consumer_health WHERE id=1")
        status = result["state"] if result else {}
        return {
            **status,
            "healthy": bool(status)
            and time.time() - status.get("heartbeat", 0) < 120
            and not status.get("error_type"),
        }

    def public_texts(self, location_id: str) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    text("""SELECT t.editorial_text,r.created_at,s.name restaurant_name
                FROM review_texts t JOIN reviews r ON r.id=t.review_id LEFT JOIN restaurants s ON s.id=t.restaurant_id
                WHERE r.location_id=:location AND t.public AND r.moderation='approved' AND r.fraud=0
                ORDER BY r.created_at DESC,r.id LIMIT 100"""),
                    {"location": location_id},
                ).mappings()
            ]

    def listing(
        self,
        query: str = "",
        moderation: str = "",
        kind: str = "",
        repeat: bool = False,
        page: int = 1,
        stage: str = "",
    ) -> dict[str, Any]:
        clauses = ["r.alex_session_id IS NULL AND r.admin_session_id IS NULL"]
        params: dict[str, Any] = {
            "q": "%" + query.replace("%", "\\%").replace("_", "\\_") + "%",
            "moderation": moderation,
            "kind": kind,
        }
        if query:
            clauses.append("(l.name ILIKE :q OR l.slug ILIKE :q)")
        if moderation in ("pending", "approved", "rejected"):
            clauses.append("r.moderation=:moderation")
        if kind in ("edit", "restaurant"):
            clauses.append(
                "EXISTS(SELECT 1 FROM proposals p WHERE p.review_id=r.id AND p.kind=:kind)"
            )
        if repeat:
            clauses.append("coalesce((v.metadata->'repeat_signals'->>'same_audio')::integer,0)>0")
        joins = (
            "FROM reviews r JOIN locations l ON l.id=r.location_id LEFT JOIN voice_links v ON v.review_id=r.id LEFT JOIN review_texts t ON t.review_id=r.id WHERE "
            + " AND ".join(clauses)
        )
        with self.engine.connect() as conn:
            count = row(conn, "SELECT count(*) n " + joins, **params)
            total = count["n"] if count else 0
            page = max(1, min(page, max(1, (total + 29) // 30)))
            params.update(offset=(page - 1) * 30)
            items = [
                dict(r)
                for r in conn.execute(
                    text(
                        "SELECT r.*,l.name location_name,l.slug location_slug,v.number,v.active_run,t.editorial_text "
                        + joins
                        + " ORDER BY r.created_at DESC,r.id LIMIT 30 OFFSET :offset"
                    ),
                    params,
                ).mappings()
            ]
        for item in items:
            item["path"] = (
                "/admin/voice/" + item["location_slug"] + "/" + str(item["number"])
                if item["number"]
                else "/admin/voice/legacy/" + str(item["id"])
            )
        if stage == "waiting-text":
            clauses.append("t.review_id IS NULL")
        elif stage == "text-ready":
            clauses.append("t.review_id IS NOT NULL")
        elif stage == "analysis-ready":
            clauses.append(
                "r.result IS NOT NULL AND (v.reference IS NULL OR EXISTS(SELECT 1 FROM voice_results a WHERE a.review_id=r.id AND a.run=v.active_run))"
            )
        elif stage == "failed":
            clauses.append("(r.error IS NOT NULL OR r.status='failed')")
        return {"rows": items, "page": page, "pages": max(1, (total + 29) // 30), "total": total}

    def resolve(self, slug: str, number: int) -> tuple[str, str]:
        with self.engine.connect() as conn:
            item = row(
                conn,
                """SELECT v.review_id,l.slug FROM voice_links v JOIN locations l ON l.id=v.location_id
                WHERE v.number=:n AND (l.slug=:slug OR l.id IN (SELECT location_id FROM location_slug_redirects WHERE slug=:slug))""",
                n=number,
                slug=slug,
            )
        if not item:
            raise NotFound("Отзыв не найден")
        return str(item["review_id"]), item["slug"]

    def detail(self, review_id: str) -> dict[str, Any]:
        with self.engine.connect() as conn:
            review = row(conn, "SELECT * FROM reviews WHERE id=:id", id=review_id)
            if not review:
                raise NotFound("Отзыв не найден")
            location = row(conn, "SELECT * FROM locations WHERE id=:id", id=review["location_id"])
            assert location is not None
            link = row(conn, "SELECT * FROM voice_links WHERE review_id=:id", id=review_id)
            text_review = row(conn, "SELECT * FROM review_texts WHERE review_id=:id", id=review_id)
            proposals = [
                dict(p)
                for p in conn.execute(
                    text("""SELECT p.*,v.run,v.revision FROM proposals p
                LEFT JOIN voice_proposals v ON v.proposal_id=p.id WHERE p.review_id=:id
                ORDER BY p.created_at,p.id"""),
                    {"id": review_id},
                ).mappings()
            ]
            decisions = [
                dict(p)
                for p in conn.execute(
                    text(
                        "SELECT * FROM review_decisions WHERE review_id=:id ORDER BY created_at,id"
                    ),
                    {"id": review_id},
                ).mappings()
            ]
            runs = [
                dict(p)
                for p in conn.execute(
                    text("SELECT * FROM voice_results WHERE review_id=:id ORDER BY run DESC"),
                    {"id": review_id},
                ).mappings()
            ]
            restaurants = [
                dict(p)
                for p in conn.execute(
                    text(
                        "SELECT id,name,phone,description,opening_hours,maps_url,cuisine FROM restaurants WHERE location_id=:id ORDER BY name,id"
                    ),
                    {"id": review["location_id"]},
                ).mappings()
            ]
            legacy_jobs = [
                dict(p)
                for p in conn.execute(
                    text(
                        "SELECT j.id,r.path FROM jobs j JOIN request_logs r ON r.job_id=j.id WHERE j.review_id=:id ORDER BY j.created_at DESC"
                    ),
                    {"id": review_id},
                ).mappings()
            ]
            analysis = next(
                (r["analysis"] for r in runs if link and r["run"] == link["active_run"]),
                review["result"] or {},
            )
            if link and analysis:
                import copy

                analysis = copy.deepcopy(analysis)
                for binding in conn.execute(
                    text(
                        "SELECT number,restaurant_id FROM review_fragment_bindings WHERE review_id=:id AND run=:run"
                    ),
                    {"id": review_id, "run": link["active_run"]},
                ).mappings():
                    analysis["entries"][binding["number"]]["restaurantId"] = (
                        str(binding["restaurant_id"]) if binding["restaurant_id"] else None
                    )
        for p in proposals:
            target = p["payload"].get("restaurantId")
            p["before"] = next((r for r in restaurants if str(r["id"]) == target), {})
            before = p["before"]
            p["changes"] = p["payload"].get("changes", p["payload"])
            p["final"] = {
                "name": before.get("name", p["payload"].get("name", "")),
                "cuisine": before.get("cuisine", ""),
                "description": before.get("description", ""),
                "phone": before.get("phone", ""),
                "mapsUrl": before.get("maps_url", ""),
                "openingHours": before.get("opening_hours"),
                **p["changes"],
            }
            p["revision"] = p["revision"] or 1
            p["run"] = p["run"] or 0
        with self.engine.connect() as conn:
            shared = conn.execute(
                text(
                    "SELECT DISTINCT l.id,l.revision FROM locations l JOIN restaurant_locations x ON x.location_id=l.id WHERE l.id<>:id AND x.restaurant_id IN (SELECT restaurant_id FROM restaurant_locations WHERE location_id=:id)"
                ),
                {"id": review["location_id"]},
            ).mappings()
            affected_revisions = {
                str(r["id"]): str(r["revision"]) if r["revision"] else None for r in shared
            }
        return {
            "affected_revisions": affected_revisions,
            "review": review,
            "text": text_review,
            "location": location,
            "link": link,
            "proposals": proposals,
            "decisions": decisions,
            "runs": runs,
            "restaurants": restaurants,
            "legacy_jobs": legacy_jobs,
            "analysis": analysis,
            "changes": review["changes"],
            "path": "/admin/voice/" + location["slug"] + "/" + str(link["number"])
            if link
            else "/admin/voice/legacy/" + review_id,
        }
