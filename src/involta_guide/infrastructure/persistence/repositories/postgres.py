import time
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from involta_guide.application.ports.repositories import Record
from involta_guide.domain.entities import RuleViolation
from involta_guide.domain.rules import KINDS

from ..models import Base

PUBLIC = """(l.listed OR l.id IN (WITH RECURSIVE useful AS (SELECT id,parent_id FROM locations WHERE active=1 AND initialized_at IS NOT NULL UNION SELECT p.id,p.parent_id FROM locations p JOIN useful u ON p.id=u.parent_id WHERE p.active=1) SELECT id FROM useful))"""


def row_dict(row: Any) -> Record:
    return {
        key: str(value) if isinstance(value, UUID) else value for key, value in dict(row).items()
    }


class PostgresRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def health(self) -> None:
        pending = self.session.execute(text("SELECT count(*) FROM beta_cleanup_files")).scalar_one()
        if pending:
            raise RuleViolation("Переход БД не завершён: выполните явную очистку Guide-аудио")

    def table(self, name: str) -> Any:
        return Base.metadata.tables[name]

    def query(self, sql: str, params: Record | None = None) -> list[Record]:
        return [row_dict(row) for row in self.session.execute(text(sql), params or {}).mappings()]

    def get(self, table: str, key: str, *, lock: bool = False) -> Record | None:
        target = self.table(table)
        primary = list(target.primary_key.columns)
        statement = select(target).where(primary[0] == key)
        if lock:
            statement = statement.with_for_update()
        row = self.session.execute(statement).mappings().first()
        return row_dict(row) if row else None

    def rows(
        self,
        table: str,
        filters: Record | None = None,
        *,
        limit: int = 200,
        order: str = "created_at",
    ) -> list[Record]:
        target = self.table(table)
        statement = select(target)
        for key, value in (filters or {}).items():
            statement = statement.where(target.c[key] == value)
        if order in target.c:
            statement = statement.order_by(target.c[order].desc())
        return [row_dict(row) for row in self.session.execute(statement.limit(limit)).mappings()]

    def add(self, table: str, values: Record) -> Record:
        target = self.table(table)
        row = (
            self.session.execute(target.insert().values(**values).returning(target))
            .mappings()
            .one()
        )
        return row_dict(row)

    def change(self, table: str, key: str, values: Record) -> None:
        target = self.table(table)
        self.session.execute(
            target.update().where(list(target.primary_key.columns)[0] == key).values(**values)
        )

    def setting(self, key: str, fallback: str = "") -> str:
        value = self.get("settings", key)
        return value["value"] if value else fallback

    def set_setting(self, key: str, value: str) -> None:
        table = self.table("settings")
        self.session.execute(
            insert(table)
            .values(key=key, value=value)
            .on_conflict_do_update(index_elements=["key"], set_={"value": value})
        )

    def audit(self, action: str, target: str, detail: str = "") -> None:
        self.add(
            "audit", {"id": str(uuid4()), "action": action, "target": str(target), "detail": detail}
        )

    def log_reference(self, job_id: str) -> Record:
        return self.query(
            "SELECT location_slug,number,path FROM request_logs WHERE job_id=:id", {"id": job_id}
        )[0]

    def generation_prompt(self, job_id: str) -> str | None:
        return self.session.execute(
            text(
                "SELECT request_json->'input'->1->>'content' FROM generation_requests WHERE job_id=:id"
            ),
            {"id": job_id},
        ).scalar_one_or_none()

    def by_slug(self, slug: str) -> tuple[Record, bool] | None:
        direct = self.query("SELECT * FROM locations WHERE slug=:slug AND active=1", {"slug": slug})
        if direct:
            return direct[0], False
        alias = self.query(
            "SELECT l.* FROM location_slug_redirects s JOIN locations l ON l.id=s.location_id WHERE s.slug=:slug AND l.active=1",
            {"slug": slug},
        )
        return (alias[0], True) if alias else None

    def tree(self, location_id: str, *, ancestors: bool = False) -> list[Record]:
        if ancestors:
            return self.query(
                """WITH RECURSIVE chain AS (SELECT l.*,0 depth,ARRAY[l.id] path FROM locations l WHERE id=:id UNION ALL SELECT l.*,c.depth+1,c.path||l.id FROM locations l JOIN chain c ON l.id=c.parent_id WHERE NOT l.id=ANY(c.path) AND c.depth<32) SELECT * FROM chain WHERE depth>0 ORDER BY depth DESC""",
                {"id": location_id},
            )
        return self.query(
            """WITH RECURSIVE tree AS (SELECT l.*,0 depth,ARRAY[l.id] path FROM locations l WHERE id=:id UNION ALL SELECT l.*,t.depth+1,t.path||l.id FROM locations l JOIN tree t ON l.parent_id=t.id WHERE NOT l.id=ANY(t.path)) SELECT * FROM tree ORDER BY depth,name""",
            {"id": location_id},
        )

    def related(self, location_id: str) -> list[Record]:
        return self.query(
            "SELECT DISTINCT l.* FROM location_relations r JOIN locations l ON l.id=CASE WHEN r.location_id=:id THEN r.related_id ELSE r.location_id END WHERE r.location_id=:id OR r.related_id=:id ORDER BY l.name",
            {"id": location_id},
        )

    def search_locations(self, **options: Any) -> Record:
        clauses = []
        params: Record = {}
        status = options.get("status", "all")
        q = str(options.get("query", "")).strip()[:100]
        kind = options.get("kind", "")
        if options.get("public_only"):
            clauses.append(PUBLIC)
        if options.get("filled_only"):
            clauses.append("l.initialized_at IS NOT NULL")
        if status in ("active", "inactive"):
            clauses.append("l.active=:active")
            params["active"] = int(status == "active")
        elif status == "empty":
            clauses.append("l.initialized_at IS NULL")
        elif status in ("published", "filled"):
            clauses.append("l.initialized_at IS NOT NULL")
        elif status == "hidden":
            clauses.append("NOT l.listed")
        if q:
            params["q"] = (
                "%"
                + q.lower()
                .replace("ё", "е")
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
                + "%"
            )
            clauses.append(
                "translate(lower(l.name||' '||l.region||' '||array_to_string(l.aliases,' ')),'ё','е') LIKE :q"
            )
        if kind in KINDS:
            clauses.append("l.kind=:kind")
            params["kind"] = kind
        if options.get("parent_id"):
            clauses.append(
                "l.id IN (WITH RECURSIVE tree AS (SELECT id FROM locations WHERE parent_id=:parent UNION ALL SELECT c.id FROM locations c JOIN tree t ON c.parent_id=t.id) SELECT id FROM tree)"
            )
            params["parent"] = options["parent_id"]
        elif options.get("roots_only") and not q and not kind:
            clauses.append("l.parent_id IS NULL")
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        total = self.query("SELECT count(*) AS n FROM locations l " + where, params)[0]["n"]
        size = max(1, min(100, int(options.get("page_size", 60))))
        pages = max(1, (total + size - 1) // size)
        page = max(1, min(pages, int(options.get("page", 1))))
        order = ""
        if q:
            params["exact"] = q.lower().replace("ё", "е")
            order = "CASE WHEN translate(lower(l.name),'ё','е')=:exact THEN 0 WHEN EXISTS(SELECT 1 FROM unnest(l.aliases) a WHERE translate(lower(a),'ё','е')=:exact) THEN 1 ELSE 2 END,"
        if options.get("admin_order"):
            order += "l.updated_at DESC NULLS LAST,"
        params.update(size=size, offset=(page - 1) * size)
        rows = self.query(
            f"SELECT l.*,p.name parent_name,(SELECT count(*) FROM locations c WHERE c.parent_id=l.id AND c.active=1) child_count FROM locations l LEFT JOIN locations p ON p.id=l.parent_id {where} ORDER BY {order}CASE l.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,l.name,l.id LIMIT :size OFFSET :offset",
            params,
        )
        return {"rows": rows, "total": total, "page": page, "pages": pages}

    def allowed(self, restaurant_id: str, location_id: str) -> bool:
        return bool(
            self.query(
                "SELECT 1 FROM restaurants WHERE id=:restaurant AND location_id=:location",
                {"restaurant": restaurant_id, "location": location_id},
            )
        )

    def link(self, table: str, values: Record) -> None:
        self.session.execute(insert(self.table(table)).values(**values).on_conflict_do_nothing())

    def replace_relations(self, location_id: str, related: list[str]) -> None:
        table = self.table("location_relations")
        self.session.execute(
            delete(table).where(
                (table.c.location_id == location_id) | (table.c.related_id == location_id)
            )
        )
        for key in set(related):
            self.link("location_relations", {"location_id": location_id, "related_id": key})

    def available(self, location_id: str) -> list[Record]:
        from involta_guide.application.services.editorial import current_content

        location = self.get("locations", location_id)
        return (
            current_content(self, location)["restaurants"]
            if location and not location["is_hub"]
            else []
        )

    def known(self, location_id: str) -> list[Record]:
        return self.query(
            "SELECT id,name,location_id FROM restaurants WHERE location_id=:id AND active=1",
            {"id": location_id},
        )

    def restaurant_cities(self, query: str) -> list[Record]:
        return self.query(
            """SELECT l.id,l.name,l.region,l.slug,l.kind,l.revision,l.initialized_at,
                count(DISTINCT r.id) restaurant_count,
                l.updated_at filled_at
            FROM locations l JOIN restaurants r ON r.location_id=l.id
            WHERE translate(lower(l.name||' '||l.region||' '||array_to_string(l.aliases,' ')),
                'ё','е') LIKE :q
            GROUP BY l.id ORDER BY filled_at DESC NULLS LAST,l.name,l.id""",
            {
                "q": "%"
                + query.strip()
                .lower()
                .replace("ё", "е")
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
                + "%"
            },
        )

    def city_restaurants(self, location_id: str) -> list[Record]:
        return self.query(
            """SELECT r.*,l.name location_name,
          (SELECT jsonb_agg(jsonb_build_object('id',v.id,'created_at',v.created_at,'moderation',v.moderation,
            'fraud',v.fraud,'transcript',v.transcript,'entries',(SELECT jsonb_agg(f.fragment)
            FROM voice_fragments f LEFT JOIN review_fragment_bindings b USING(review_id,run,number)
            WHERE f.review_id=v.id AND f.run=vl.active_run AND CASE WHEN b.review_id IS NOT NULL
            THEN b.restaurant_id ELSE f.restaurant_id END=r.id)))
           FROM reviews v JOIN voice_links vl ON vl.review_id=v.id WHERE v.location_id=l.id AND v.status<>'deleted' AND EXISTS(
            SELECT 1 FROM voice_fragments f LEFT JOIN review_fragment_bindings b USING(review_id,run,number)
            WHERE f.review_id=v.id AND f.run=vl.active_run AND CASE WHEN b.review_id IS NOT NULL
            THEN b.restaurant_id ELSE f.restaurant_id END=r.id)) reviews,
          jsonb_build_array(jsonb_build_object('id',l.id,'name',l.name,'slug',l.slug)) collections
          FROM restaurants r JOIN locations l ON l.id=r.location_id WHERE r.location_id=:id
          ORDER BY r.number""",
            {"id": location_id},
        )

    def summary(self) -> Record:
        result = self.query(
            "SELECT count(*) total,count(*) FILTER (WHERE initialized_at IS NULL) empty,count(*) FILTER (WHERE initialized_at IS NOT NULL) published FROM locations"
        )[0]
        result["reviews"] = self.query("SELECT count(*) n FROM reviews WHERE status<>'deleted'")[0][
            "n"
        ]
        result["jobs"] = self.query("SELECT status,count(*) n FROM jobs GROUP BY status")
        result["usage"] = self.query(
            "SELECT count(*) calls,coalesce(sum(input_tokens),0) input_tokens,coalesce(sum(output_tokens),0) output_tokens FROM ai_usage WHERE created_at>=CURRENT_DATE"
        )[0]
        return result

    def phone_proposals(self) -> list[Record]:
        return self.query(
            """SELECT p.restaurant_id restaurant_id,p.payload->>'phone' phone,max(r.name) restaurant_name,max(r.phone) current_phone,count(*) messages,count(DISTINCT v.visitor) visitors,count(DISTINCT v.ip) networks,count(*) FILTER (WHERE v.fraud=1) fraud,count(*) FILTER (WHERE v.moderation='approved' AND v.fraud=0) approved,jsonb_agg(jsonb_build_object('reviewId',v.id,'date',v.created_at,'reason',p.payload->>'reason','status',p.status)) evidence FROM proposals p JOIN reviews v ON v.id=p.review_id LEFT JOIN restaurants r ON r.id=p.restaurant_id WHERE v.status<>'deleted' AND coalesce(p.payload->>'phone','')<>'' GROUP BY p.restaurant_id,p.payload->>'phone' ORDER BY max(v.created_at) DESC LIMIT 200"""
        )

    def queue(self, values: Record) -> bool:
        statement = (
            insert(self.table("jobs"))
            .values(**values)
            .on_conflict_do_nothing()
            .returning(self.table("jobs").c.id)
        )
        return self.session.execute(statement).first() is not None

    def rate_limit(self, key: str, limit: int, seconds: int) -> None:
        now = int(time.time() * 1000)
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,1))"), {"key": key}
        )
        old = self.get("login_attempts", key, lock=True)
        if old and old["reset_at"] > now and old["count"] >= limit:
            raise RuleViolation("Слишком много запросов. Попробуйте позже.")
        values = {
            "key": key,
            "count": old["count"] + 1 if old and old["reset_at"] > now else 1,
            "reset_at": old["reset_at"] if old and old["reset_at"] > now else now + seconds * 1000,
        }
        self.session.execute(
            insert(self.table("login_attempts"))
            .values(**values)
            .on_conflict_do_update(
                index_elements=["key"],
                set_={"count": values["count"], "reset_at": values["reset_at"]},
            )
        )

    def lease_valid(self, job_id: str, owner: str) -> bool:
        return bool(
            self.query(
                "SELECT id FROM jobs WHERE id=:id AND lease_owner=:owner AND status='running' AND lease_until>now() FOR UPDATE",
                {"id": job_id, "owner": owner},
            )
        )
