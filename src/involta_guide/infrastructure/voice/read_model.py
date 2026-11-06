import re
from typing import Any

from sqlalchemy import Engine, text

from involta_guide.domain.entities import NotFound
from involta_guide.infrastructure.persistence.repositories.public_catalogue import PUBLIC
from involta_voice.db import row


def browser_label(agent: str) -> str:
    browser = next(
        (
            label + " " + match.group(1)
            for token, label in (
                ("Edg", "Edge"),
                ("OPR", "Opera"),
                ("Chrome", "Chrome"),
                ("Firefox", "Firefox"),
                ("Version", "Safari"),
            )
            if (match := re.search(token + r"/(\d+)", agent))
        ),
        "",
    )
    platform = next(
        (
            label
            for token, label in (
                ("Android", "Android"),
                ("iPhone", "iPhone"),
                ("iPad", "iPad"),
                ("Windows", "Windows"),
                ("Macintosh", "macOS"),
                ("Linux", "Linux"),
            )
            if token in agent
        ),
        "",
    )
    return " · ".join(p for p in (browser, platform) if p) or agent[:80] or "Браузер не указан"


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

    def public_texts(self, location_id: int) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    text("""SELECT id::text reference,
                coalesce(edited_text,transcript) transcript,created_at,number,publication_status,
                CASE WHEN status='transcribed' THEN 'result_ready' ELSE 'accepted' END state
                FROM reviews WHERE location_id=:location AND publication_status=1 AND status IN ('received','transcribed')
                ORDER BY created_at DESC,number DESC LIMIT 10"""),
                    {"location": location_id},
                ).mappings()
            ]

    def latest_public_texts(self) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            return [
                dict(item)
                for item in conn.execute(
                    text(f"""SELECT coalesce(r.edited_text,r.transcript) transcript,r.created_at,r.number,
                    l.name location_name,l.slug location_slug
                    FROM reviews r JOIN locations l ON l.id=r.location_id
                    WHERE r.publication_status=1 AND r.status IN ('received','transcribed')
                    AND l.active=1 AND {PUBLIC}
                    ORDER BY r.created_at DESC,r.number DESC LIMIT 10""")
                ).mappings()
            ]

    def publication_states(self, location_id: int, references: list[str]) -> dict[str, int]:
        if not references:
            return {}
        with self.engine.connect() as conn:
            return {
                str(r["id"]): r["publication_status"]
                for r in conn.execute(
                    text(
                        "SELECT id,publication_status FROM reviews WHERE location_id=:location AND id=ANY(CAST(:references AS uuid[]))"
                    ),
                    {"location": location_id, "references": references},
                ).mappings()
            }

    def cities(self, query: str = "") -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    text("""SELECT l.id,l.slug,l.name,count(*) total,max(r.created_at) latest
                FROM locations l JOIN reviews r ON r.location_id=l.id
                WHERE r.status<>'deleted' AND (l.name ILIKE :q OR l.slug ILIKE :q)
                GROUP BY l.id ORDER BY latest DESC,l.name,l.id"""),
                    {
                        "q": "%"
                        + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                        + "%"
                    },
                ).mappings()
            ]

    def listing(self, slug: str | None = None, query: str = "", page: int = 1) -> dict[str, Any]:
        cities = self.cities(query)
        with self.engine.connect() as conn:
            city = (
                row(conn, "SELECT id,slug,name FROM locations WHERE slug=:slug", slug=slug)
                if slug
                else (cities[0] if cities else None)
            )
            if slug and not city:
                raise NotFound("Локация не найдена")
            counted = (
                row(
                    conn,
                    "SELECT count(*) n FROM reviews WHERE location_id=:id AND status<>'deleted'",
                    id=city["id"],
                )
                if city
                else None
            )
            total = counted["n"] if counted else 0
            pages = max(1, (total + 19) // 20)
            page = max(1, min(page, pages))
            items = (
                [
                    dict(r)
                    for r in conn.execute(
                        text("""SELECT r.*,v.reference FROM reviews r
                LEFT JOIN voice_links v ON v.review_id=r.id WHERE r.location_id=:id AND r.status<>'deleted'
                ORDER BY r.created_at DESC,r.number DESC LIMIT 20 OFFSET :offset"""),
                        {"id": city["id"], "offset": (page - 1) * 20},
                    ).mappings()
                ]
                if city
                else []
            )
        for item in items:
            assert city is not None
            item["path"] = "/admin/reviews/" + city["slug"] + "/" + str(item["number"])
            item["log_path"] = "/logs/reviews/" + city["slug"] + "/" + str(item["number"])
            item["browser"] = browser_label(item["user_agent"])
            item["reference"] = str(item["reference"]) if item["reference"] else None
        return {
            "cities": cities,
            "city": city,
            "rows": items,
            "page": page,
            "pages": pages,
            "total": total,
        }

    def retained(self, after: int, limit: int) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    text("""SELECT r.id,r.number,r.created_at,
                r.transcript,r.publication_status,r.duration_seconds,l.slug,l.name location_name,
                v.reference FROM reviews r JOIN locations l ON l.id=r.location_id
                LEFT JOIN voice_links v ON v.review_id=r.id
                WHERE r.status<>'deleted' AND r.number>:after ORDER BY r.number LIMIT :limit"""),
                    {"after": after, "limit": limit},
                ).mappings()
            ]

    def path(self, review_id: str) -> str:
        with self.engine.connect() as conn:
            item = row(
                conn,
                "SELECT r.number,l.slug FROM reviews r JOIN locations l ON l.id=r.location_id WHERE r.id=:id",
                id=review_id,
            )
        if not item:
            raise NotFound("Отзыв не найден")
        return "/admin/reviews/" + item["slug"] + "/" + str(item["number"])

    def resolve(self, slug: str, number: int) -> tuple[str, str]:
        with self.engine.connect() as conn:
            item = row(
                conn,
                "SELECT r.id FROM reviews r JOIN locations l ON l.id=r.location_id WHERE l.slug=:slug AND r.number=:n",
                slug=slug,
                n=number,
            )
        if not item:
            raise NotFound("Отзыв не найден")
        return str(item["id"]), slug

    def detail(self, review_id: str) -> dict[str, Any]:
        with self.engine.connect() as conn:
            review = row(conn, "SELECT * FROM reviews WHERE id=:id", id=review_id)
            if not review:
                raise NotFound("Отзыв не найден")
            location = row(conn, "SELECT * FROM locations WHERE id=:id", id=review["location_id"])
            link = row(conn, "SELECT * FROM voice_links WHERE review_id=:id", id=review_id)
            events = [
                dict(r)
                for r in conn.execute(
                    text("SELECT * FROM voice_inbox WHERE review_id=:id ORDER BY sequence"),
                    {"id": review_id},
                ).mappings()
            ]
            actions = [
                dict(r)
                for r in conn.execute(
                    text(
                        "SELECT action,detail,created_at FROM audit WHERE target=:id AND action IN ('review_deleted','review_reprocess') ORDER BY created_at"
                    ),
                    {"id": review_id},
                ).mappings()
            ]
        return {
            "review": review,
            "location": location,
            "link": link,
            "events": events,
            "actions": actions,
        }
