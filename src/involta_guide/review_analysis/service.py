from typing import Any
from uuid import uuid4

from sqlalchemy import Connection, Engine, text

from involta_guide.config import Settings
from involta_guide.domain.entities import Conflict, NotFound, RuleViolation
from involta_guide.infrastructure.ai.background import BackgroundGeneration
from involta_guide.infrastructure.ai.openai import OpenAIProvider
from involta_voice.db import encoded, execute, row

from .names import restore_restaurant_names
from .schema import Analysis


class ReviewAnalysis:
    def __init__(self, engine: Engine, settings: Settings) -> None:
        self.engine, self.settings = engine, settings

    def schedule(self) -> int:
        """Only the worker enqueues ready transcripts. GET requests never schedule work."""
        prompt = (self.settings.root_dir / "data/prompts/review-analysis-v1.txt").read_text()
        with self.engine.begin() as conn:
            if not conn.execute(text("SELECT pg_try_advisory_xact_lock(47031015)")).scalar():
                return 0
            candidates = (
                conn.execute(
                    text("""SELECT r.id,r.location_id,r.transcript,l.name,l.slug,l.content_epoch
              FROM reviews r JOIN locations l ON l.id=r.location_id
              LEFT JOIN review_analyses a ON a.review_id=r.id
              WHERE r.status='transcribed' AND length(trim(r.transcript))>0
                AND NOT l.reset_pending AND NOT l.is_hub AND r.content_epoch=l.content_epoch
                AND a.manual_category IS NULL
                AND (a.review_id IS NULL OR a.source_text IS DISTINCT FROM r.transcript)
              ORDER BY r.created_at,r.id LIMIT 20 FOR UPDATE OF l,r SKIP LOCKED""")
                )
                .mappings()
                .all()
            )
            for review in candidates:
                restaurants = [
                    dict(r)
                    for r in conn.execute(
                        text(
                            "SELECT id::text,name FROM restaurants WHERE location_id=:id ORDER BY number"
                        ),
                        {"id": review["location_id"]},
                    ).mappings()
                ]
                job_id = str(uuid4())
                payload = {
                    "review_text": review["transcript"],
                    "location": {"id": review["location_id"], "name": review["name"]},
                    "restaurants": restaurants,
                }
                execute(
                    conn,
                    """INSERT INTO jobs(id,kind,location_id,review_id,content_epoch,prompt,model,public_log,report)
                    VALUES(:id,'review_analysis',:loc,:review,:epoch,:prompt,:model,false,CAST(:report AS jsonb))""",
                    id=job_id,
                    loc=review["location_id"],
                    review=review["id"],
                    epoch=review["content_epoch"],
                    prompt=prompt,
                    model=self.settings.openai_model,
                    report=encoded({"input": payload}),
                )
                execute(
                    conn,
                    """INSERT INTO review_analyses(review_id,job_id,source_text)
                    VALUES(:review,:job,:source) ON CONFLICT(review_id) DO UPDATE SET
                    job_id=excluded.job_id,source_text=excluded.source_text,result=NULL,approved_at=NULL,approved_by=NULL""",
                    review=review["id"],
                    job=job_id,
                    source=review["transcript"],
                )
            return len(candidates)

    def process(self, job: dict[str, Any], provider: OpenAIProvider) -> None:
        with self.engine.connect() as conn:
            current = row(
                conn,
                """SELECT 1 FROM review_analyses a JOIN reviews r ON r.id=a.review_id
                JOIN locations l ON l.id=r.location_id
                WHERE a.job_id=:id AND a.source_text=r.transcript AND r.status='transcribed'
                AND NOT l.reset_pending AND l.content_epoch=:epoch""",
                id=job["id"],
                epoch=job["content_epoch"],
            )
        if not current:
            raise Conflict("Отзыв удалён, изменён или локация сброшена")
        payload = job["report"]["input"]
        response = BackgroundGeneration(provider).advance(
            job["id"],
            {
                "model": job["model"],
                "background": True,
                "store": True,
                "max_output_tokens": 5000,
                "input": [
                    {"role": "system", "content": job["prompt"]},
                    {"role": "user", "content": encoded(payload)},
                ],
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "review_analysis_v2",
                        "strict": True,
                        "schema": Analysis.response_schema(),
                    }
                },
            },
        )
        output = "".join(
            p.get("text", "")
            for item in response.get("output", [])
            for p in item.get("content", [])
            if p.get("type") == "output_text"
        )
        result = Analysis.model_validate_json(output)
        known = {r["id"] for r in payload["restaurants"]}
        seen: set[str] = set()
        for mention in result.mentions:
            if mention.evidence not in payload["review_text"]:
                raise RuleViolation("Цитата ИИ отсутствует в исходном отзыве")
            identity = (
                str(mention.restaurant_id)
                if mention.restaurant_id
                else mention.name.strip().casefold()
            )
            if not mention.name.strip() or identity in seen:
                raise RuleViolation("Пустое или повторное упоминание ресторана")
            seen.add(identity)
            if mention.restaurant_id and str(mention.restaurant_id) not in known:
                raise RuleViolation("ИИ вернул ресторан вне переданного каталога")
            if mention.suggested and not mention.restaurant_id and mention.sentiment != 1:
                raise RuleViolation("Предложение нового ресторана должно содержать голос +1")
        result = restore_restaurant_names(result, payload["review_text"], payload["restaurants"])
        with self.engine.begin() as conn:
            self._lock_location(conn, job["review_id"])
            review = row(conn, "SELECT * FROM reviews WHERE id=:id FOR UPDATE", id=job["review_id"])
            live = row(
                conn,
                """SELECT j.id FROM jobs j JOIN review_analyses a ON a.job_id=j.id
                WHERE j.id=:id AND j.status='running' AND j.lease_owner=:owner AND j.lease_until>now()
                AND a.source_text=:source FOR UPDATE OF j,a""",
                id=job["id"],
                owner=job["lease_owner"],
                source=review["transcript"] if review else None,
            )
            if not live or not review or review["status"] != "transcribed":
                raise Conflict("Результат обработки устарел")
            execute(
                conn,
                "UPDATE review_analyses SET result=CAST(:result AS jsonb) WHERE job_id=:id",
                id=job["id"],
                result=result.model_dump_json(),
            )
            execute(
                conn,
                """UPDATE jobs SET status='done',finished_at=now(),lease_owner=NULL,lease_until=NULL,
                report=report||jsonb_build_object('result',CAST(:result AS jsonb)) WHERE id=:id""",
                id=job["id"],
                result=result.model_dump_json(),
            )

    @staticmethod
    def _lock_location(conn: Connection, review_id: str) -> dict[str, Any]:
        location = row(
            conn,
            "SELECT l.* FROM locations l JOIN reviews r ON r.location_id=l.id WHERE r.id=:id FOR UPDATE OF l",
            id=review_id,
        )
        if not location:
            raise NotFound("Отзыв не найден")
        if location["reset_pending"]:
            raise Conflict("Локация очищается")
        return location

    def approve(self, review_id: str, job_id: str, actor: str) -> None:
        with self.engine.begin() as conn:
            location = self._lock_location(conn, review_id)
            review = row(conn, "SELECT * FROM reviews WHERE id=:id FOR UPDATE", id=review_id)
            analysis = row(
                conn, "SELECT * FROM review_analyses WHERE review_id=:id FOR UPDATE", id=review_id
            )
            if not review or review["status"] == "deleted":
                raise NotFound("Отзыв удалён")
            if (
                not analysis
                or str(analysis["job_id"]) != job_id
                or not analysis["result"]
                or analysis["source_text"] != review["transcript"]
                or review["content_epoch"] != location["content_epoch"]
            ):
                raise Conflict("Результат ещё не готов или устарел. Обновите страницу.")
            if analysis["manual_category"]:
                raise Conflict("Категория отзыва задана вручную")
            if analysis["approved_at"]:
                return
            result = Analysis.model_validate(analysis["result"])
            applied: dict[str, int] = {}
            resolved = []
            if result.category == "ok":
                for mention in result.mentions:
                    restaurant = None
                    if mention.restaurant_id:
                        restaurant = row(
                            conn,
                            "SELECT id,name FROM restaurants WHERE id=:id AND location_id=:loc",
                            id=str(mention.restaurant_id),
                            loc=location["id"],
                        )
                        if not restaurant:
                            raise Conflict("Ресторан из предложения больше не доступен")
                    elif mention.suggested:
                        # The location lock serializes approvals; exact names reuse one catalogue row.
                        restaurant = row(
                            conn,
                            "SELECT id,name FROM restaurants WHERE location_id=:loc AND lower(trim(name))=lower(trim(:name)) ORDER BY number LIMIT 1",
                            loc=location["id"],
                            name=mention.name,
                        )
                        if not restaurant:
                            restaurant = {"id": str(uuid4()), "name": mention.name.strip()}
                            execute(
                                conn,
                                """INSERT INTO restaurants(id,location_id,name,cuisine,description,maps_url,phone,active)
                                VALUES(:id,:loc,:name,NULL,NULL,NULL,NULL,0)""",
                                id=restaurant["id"],
                                loc=location["id"],
                                name=restaurant["name"],
                            )
                    resolved.append(
                        {
                            **mention.model_dump(mode="json"),
                            "restaurant_id": str(restaurant["id"]) if restaurant else None,
                        }
                    )
                    if restaurant and mention.sentiment is not None:
                        key = str(restaurant["id"])
                        if key in applied and applied[key] != mention.sentiment:
                            raise RuleViolation("Противоречащие оценки одного ресторана")
                        applied[key] = mention.sentiment
            execute(conn, "DELETE FROM review_restaurant_votes WHERE review_id=:id", id=review_id)
            for restaurant_id, value in applied.items():
                execute(
                    conn,
                    "INSERT INTO review_restaurant_votes(review_id,restaurant_id,value) VALUES(:review,:restaurant,:value)",
                    review=review_id,
                    restaurant=restaurant_id,
                    value=value,
                )
            execute(
                conn,
                "UPDATE reviews SET author_name=:name,edited_text=:text,publication_status=CASE WHEN :text='' THEN 0 ELSE publication_status END WHERE id=:id",
                id=review_id,
                name=result.author_name,
                text=result.edited_text,
            )
            execute(
                conn,
                "UPDATE review_analyses SET approved_at=now(),approved_by=:actor,result=CAST(:result AS jsonb) WHERE review_id=:id",
                id=review_id,
                actor=actor,
                result=encoded(
                    {
                        **result.model_dump(mode="json"),
                        "mentions": resolved
                        if result.category == "ok"
                        else result.model_dump(mode="json")["mentions"],
                    }
                ),
            )
            execute(conn, "UPDATE jobs SET applied=true WHERE id=:id", id=job_id)
            self._audit(
                conn,
                review_id,
                "review_analysis_approved",
                {
                    "actor": actor,
                    "job": job_id,
                    "before": review["transcript"],
                    "after": result.edited_text,
                    "mentions": resolved,
                    "votes": applied,
                },
            )

    def mark_bug(self, review_id: str, actor: str) -> None:
        self._mark_category(review_id, actor, "bugs")

    def mark_humor(self, review_id: str, actor: str) -> None:
        self._mark_category(review_id, actor, "humor")

    def _mark_category(self, review_id: str, actor: str, category: str) -> None:
        with self.engine.begin() as conn:
            self._lock_location(conn, review_id)
            review = row(conn, "SELECT * FROM reviews WHERE id=:id FOR UPDATE", id=review_id)
            analysis = row(
                conn, "SELECT * FROM review_analyses WHERE review_id=:id FOR UPDATE", id=review_id
            )
            if not review or review["status"] == "deleted":
                raise NotFound("Отзыв удалён")
            if analysis and analysis["manual_category"] == category:
                return
            if (
                category == "bugs"
                and analysis
                and analysis["approved_at"]
                and analysis["source_text"] == review["transcript"]
            ):
                raise Conflict("Ручной перенос доступен для новых отзывов")
            removed_votes = [
                dict(v)
                for v in conn.execute(
                    text(
                        "DELETE FROM review_restaurant_votes WHERE review_id=:id RETURNING restaurant_id,value"
                    ),
                    {"id": review_id},
                ).mappings()
            ]
            execute(
                conn,
                """INSERT INTO review_analyses(review_id,source_text,manual_category)
                VALUES(:id,:source,:category) ON CONFLICT(review_id) DO UPDATE SET manual_category=excluded.manual_category""",
                id=review_id,
                source=review["transcript"] or "",
                category=category,
            )
            self._audit(
                conn,
                review_id,
                "review_marked_bug" if category == "bugs" else "review_marked_humor",
                {
                    "actor": actor,
                    "before": analysis["result"] if analysis else None,
                    "after": category,
                    "previous_manual_category": analysis["manual_category"] if analysis else None,
                    "removed_votes": removed_votes,
                },
            )

    def delete(self, review_id: str, actor: str) -> None:
        with self.engine.begin() as conn:
            self._lock_location(conn, review_id)
            removed = row(
                conn,
                "UPDATE reviews SET status='deleted',publication_status=0 WHERE id=:id AND status<>'deleted' RETURNING id",
                id=review_id,
            )
            if removed:
                self._audit(conn, review_id, "review_deleted", {"actor": actor})

    @staticmethod
    def _audit(conn: Connection, review_id: str, action: str, detail: dict[str, Any]) -> None:
        execute(
            conn,
            "INSERT INTO audit(id,action,target,detail) VALUES(:id,:action,:target,:detail)",
            id=str(uuid4()),
            action=action,
            target=review_id,
            detail=encoded(detail),
        )
