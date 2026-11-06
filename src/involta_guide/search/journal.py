"""One row per settled query. Late server/event delivery must never erase a click."""

import ipaddress
import json
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from .matching import VERSION

logger = logging.getLogger(__name__)

LABELLED = """WITH labelled AS (
  SELECT *, shown_at IS NOT NULL AND shown_complete AND NOT client_error
            AND jsonb_array_length(shown)=0 empty,
    shown_at IS NOT NULL AND clicked_id IS NULL
      AND (end_reason IS NOT NULL OR updated_at<now()-interval '30 seconds') no_click,
    CASE WHEN clicked_id IS NOT NULL THEN 'selected'
         WHEN client_error OR server_state='error' THEN 'error'
         WHEN shown_at IS NOT NULL AND shown_complete AND jsonb_array_length(shown)=0 THEN 'empty'
         WHEN end_reason='refined' THEN 'refined'
         WHEN shown_at IS NOT NULL AND
              (end_reason IS NOT NULL OR updated_at<now()-interval '30 seconds') THEN 'no_click'
         WHEN end_reason IS NOT NULL THEN 'not_shown'
         ELSE 'pending' END outcome
  FROM search_attempts WHERE (:include_admin OR NOT is_admin)
) """


def safe_ip(value: str) -> str | None:
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def masked_ip(value: str | None) -> str | None:
    if not value:
        return None
    address = ipaddress.ip_address(value)
    return str(
        ipaddress.ip_network(f"{address}/{24 if address.version == 4 else 48}", strict=False)
    )


class Journal:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def server(
        self,
        token: UUID,
        query: str,
        ip: str,
        is_admin: bool,
        results: list[dict[str, Any]],
        elapsed: float,
        *,
        error: bool = False,
    ) -> None:
        params = dict(
            token=str(token),
            query=query,
            ip=safe_ip(ip),
            is_admin=is_admin,
            results=json.dumps(results, ensure_ascii=False),
            elapsed=elapsed,
            state="error" if error else "ready",
            version=VERSION,
        )
        self._write(
            """INSERT INTO search_attempts
          (token,query,ip,is_admin,version,server_state,server_results,server_ms)
          VALUES(:token,:query,CAST(:ip AS inet),:is_admin,:version,:state,CAST(:results AS jsonb),:elapsed)
          ON CONFLICT(token) DO UPDATE SET
            server_state=excluded.server_state,server_results=excluded.server_results,
            server_ms=excluded.server_ms,version=excluded.version,
            is_admin=search_attempts.is_admin OR excluded.is_admin,updated_at=now()
          WHERE search_attempts.query=excluded.query""",
            params,
        )

    def event(
        self, token: UUID, query: str, ip: str, is_admin: bool, event: dict[str, Any]
    ) -> None:
        shown = event["shown"]
        params = dict(
            token=str(token),
            query=query,
            ip=safe_ip(ip),
            is_admin=is_admin,
            version=VERSION,
            shown=json.dumps(shown or [], ensure_ascii=False),
            seen=shown is not None,
            complete=event["complete"],
            shown_version=event["version"],
            seq=event["seq"],
            reason=event["reason"],
            clicked=event["clicked_id"],
            cache_hit=event["cache_hit"],
            client_error=event["client_error"],
        )
        self._write(
            """INSERT INTO search_attempts
          (token,query,ip,is_admin,version,shown,shown_at,shown_version,shown_complete,event_seq,end_reason,
           clicked_id,cache_hit,client_error)
          VALUES(:token,:query,CAST(:ip AS inet),:is_admin,:version,CAST(:shown AS jsonb),
                 CASE WHEN :seen THEN now() END,CASE WHEN :seen THEN :shown_version END,:complete,:seq,:reason,:clicked,:cache_hit,:client_error)
          ON CONFLICT(token) DO UPDATE SET
            shown=CASE WHEN :seen THEN excluded.shown ELSE search_attempts.shown END,
            shown_at=CASE WHEN :seen THEN coalesce(search_attempts.shown_at,now()) ELSE search_attempts.shown_at END,
            shown_version=CASE WHEN :seen THEN excluded.shown_version ELSE search_attempts.shown_version END,
            shown_complete=CASE WHEN :seen THEN excluded.shown_complete ELSE search_attempts.shown_complete END,
            clicked_id=coalesce(search_attempts.clicked_id,excluded.clicked_id),
            end_reason=CASE WHEN search_attempts.clicked_id IS NOT NULL THEN 'select' ELSE excluded.end_reason END,
            event_seq=excluded.event_seq,cache_hit=excluded.cache_hit,client_error=excluded.client_error,
            is_admin=search_attempts.is_admin OR excluded.is_admin,updated_at=now()
          WHERE search_attempts.query=excluded.query AND excluded.event_seq>search_attempts.event_seq""",
            params,
        )

    def _write(self, statement: str, params: dict[str, Any]) -> None:
        try:
            with self.engine.begin() as connection:
                connection.execute(text(statement), params)
        except SQLAlchemyError:
            # Search and navigation remain usable if diagnostic storage is unavailable.
            logger.warning("Could not store search diagnostics", exc_info=True)

    def report(
        self,
        *,
        before: int | None = None,
        limit: int = 100,
        outcome: str = "all",
        include_admin: bool = False,
    ) -> dict[str, Any]:
        params = dict(before=before, limit=limit, outcome=outcome, include_admin=include_admin)
        columns = """id,query,host(ip) ip,is_admin,created_at,version,server_state,server_ms,
          server_results,shown,shown_at,shown_version,shown_complete,cache_hit,client_error,end_reason,
          clicked_id,outcome,empty,no_click"""
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        LABELLED
                        + f"""SELECT {columns} FROM labelled
              WHERE (CAST(:before AS bigint) IS NULL OR id<:before)
                AND (:outcome='all' OR (:outcome='no_click' AND no_click)
                  OR (:outcome<>'no_click' AND outcome=:outcome))
              ORDER BY id DESC LIMIT :limit"""
                    ),
                    params,
                )
                .mappings()
                .all()
            )
            totals = dict(
                connection.execute(
                    text(
                        LABELLED
                        + """SELECT count(*) searches,
              count(*) FILTER(WHERE empty) empty,
              count(*) FILTER(WHERE no_click) no_click,
              count(*) FILTER(WHERE clicked_id IS NOT NULL) selected,
              count(*) FILTER(WHERE client_error OR server_state='error') errors,
              count(*) FILTER(WHERE end_reason='refined') refined FROM labelled"""
                    ),
                    params,
                )
                .mappings()
                .one()
            )
            queries = (
                connection.execute(
                    text(
                        LABELLED
                        + """SELECT lower(query) query,count(*) searches,
              count(*) FILTER(WHERE empty) empty,count(*) FILTER(WHERE no_click) no_click
              FROM labelled GROUP BY lower(query)
              ORDER BY count(*) FILTER(WHERE empty OR no_click) DESC,count(*) DESC,lower(query)
              LIMIT 20"""
                    ),
                    params,
                )
                .mappings()
                .all()
            )
        items = []
        for row in rows:
            item = dict(row)
            item["ip"] = masked_ip(item["ip"])
            item["clicked_position"] = next(
                (
                    i + 1
                    for i, result in enumerate(item["shown"])
                    if result["id"] == item["clicked_id"]
                ),
                None,
            )
            items.append(item)
        return dict(
            version=VERSION,
            totals=totals,
            queries=[dict(row) for row in queries],
            attempts=items,
            next_before=items[-1]["id"] if len(items) == limit else None,
        )
