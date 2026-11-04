from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from involta_guide.application.ports.repositories import Record
from involta_guide.domain.location_search import russian_layout, search_text

PUBLIC = """(l.listed OR l.id IN (WITH RECURSIVE useful AS (
 SELECT id,parent_id FROM locations WHERE active=1 AND initialized_at IS NOT NULL
 UNION SELECT p.id,p.parent_id FROM locations p JOIN useful u ON p.id=u.parent_id
 WHERE p.active=1) SELECT id FROM useful))"""


def normalized(expression: str) -> str:
    return f"trim(regexp_replace(translate(lower({expression}),'ё','е'),'[^[:alnum:]]+',' ','g'))"


def public_location_search(session: Session, query: str, page: int | None = None) -> Record:
    """The public form and its short suggestions share matching and ordering."""
    query = search_text(query)
    empty = {"rows": [], "total": 0, "page": 1, "pages": 1}
    if not query:
        return empty
    session.execute(text("SET LOCAL statement_timeout='500ms'"))

    def find(value: str, *, fuzzy: bool = False) -> Record:
        params: dict[str, Any] = {"q": value}
        if fuzzy:
            # This expression matches the GIN index. Aliases/context never weaken typo relevance.
            source = f"""SELECT l.id,l.name,l.slug,l.region,l.kind,p.name parent_name,
              l.initialized_at IS NOT NULL ready,l.priority,
              public.similarity(translate(lower(l.name),'ё','е'),:q) score,6 rank
              FROM locations l LEFT JOIN locations p ON p.id=l.parent_id
              WHERE l.active=1 AND {PUBLIC}
              AND translate(lower(l.name),'ё','е') OPERATOR(public.%) :q
              AND public.similarity(translate(lower(l.name),'ё','е'),:q)>=0.45"""
        else:
            name_prefix, alias_prefix, name_match, anywhere = [], [], [], []
            for i, word in enumerate(value.split()):
                params.update(
                    {f"p{i}": word + "%", f"w{i}": "% " + word + "%", f"c{i}": "%" + word + "%"}
                )
                name_prefix.append(f"(n LIKE :p{i} OR n LIKE :w{i})")
                alias_prefix.append(
                    f"EXISTS(SELECT 1 FROM unnest(a) v WHERE v LIKE :p{i} OR v LIKE :w{i})"
                )
                name_match.append(
                    f"(n LIKE :c{i} OR EXISTS(SELECT 1 FROM unnest(a) v WHERE v LIKE :c{i}))"
                )
                anywhere.append(f"corpus LIKE :c{i}")
            source = f"""SELECT *,0.0 score,CASE WHEN n=:q THEN 0
              WHEN :q=ANY(a) THEN 1 WHEN {" AND ".join(name_prefix)} THEN 2
              WHEN {" AND ".join(alias_prefix)} THEN 3
              WHEN {" AND ".join(name_match)} THEN 4 ELSE 5 END rank
              FROM (
                SELECT l.id,l.name,l.slug,l.region,l.kind,p.name parent_name,
                l.initialized_at IS NOT NULL ready,l.priority,
                {normalized("l.name")} n,
                ARRAY(SELECT {normalized("v")} FROM unnest(l.aliases) v) a,
                {normalized("l.name||' '||l.slug||' '||l.region||' '||coalesce(p.name,'')||' '||array_to_string(l.aliases,' ')")} corpus
                FROM locations l LEFT JOIN locations p ON p.id=l.parent_id
                WHERE l.active=1 AND {PUBLIC}
              ) candidates WHERE {" AND ".join(anywhere)}"""
        cte = "WITH matched AS (" + source + ") "
        result: Record = {}
        if page is not None:
            total = session.execute(text(cte + "SELECT count(*) FROM matched"), params).scalar_one()
            pages = max(1, (total + 59) // 60)
            current = max(1, min(pages, page))
            result = {"total": total, "pages": pages, "page": current}
            params.update(size=60, offset=(current - 1) * 60)
        else:
            params.update(size=6, offset=0)
        rows = session.execute(
            text(
                cte
                + """SELECT name,slug,region,kind,parent_name
          FROM matched ORDER BY rank,score DESC,ready DESC,
          CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,name,id
          LIMIT :size OFFSET :offset"""
            ),
            params,
        ).mappings()
        return {**result, "rows": [dict(row) for row in rows]}

    result = find(query)
    if result["rows"]:
        return result
    alternate = russian_layout(query)
    if alternate != query:
        result = find(alternate)
        if result["rows"]:
            return result
    if len(alternate) >= 4:
        return find(alternate, fuzzy=True)
    return result
