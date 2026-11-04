"""Small PostgreSQL audience counters, independent of Guide's HTTP and Voice code."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import Connection, Engine, text

DAYS = 30
PROJECT_TIMEZONE = timezone(timedelta(hours=3), "UTC+3")


def calendar_day(at: datetime | None = None) -> date:
    value = at or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(PROJECT_TIMEZONE).date()


@dataclass(frozen=True)
class DailyStats:
    day: date
    hits: int = 0
    uniques: int = 0
    reviews: int = 0

    @property
    def hits_per_unique(self) -> float | None:
        return self.hits / self.uniques if self.uniques else None


def _increment(
    connection: Connection,
    visitor: UUID,
    day: date,
    today: date,
    *,
    hits: int = 0,
    reviews: int = 0,
) -> None:
    start = today - timedelta(days=DAYS - 1)
    if not start <= day <= today:
        return
    connection.execute(
        text("""WITH expired AS (
          DELETE FROM audience_daily WHERE day<:start
        )
        INSERT INTO audience_daily(day,visitor_id,hits,reviews)
        VALUES(:day,:visitor,:hits,:reviews)
        ON CONFLICT(day,visitor_id) DO UPDATE SET
          hits=audience_daily.hits+excluded.hits,
          reviews=audience_daily.reviews+excluded.reviews"""),
        {"start": start, "day": day, "visitor": visitor, "hits": hits, "reviews": reviews},
    )


def add_hit(engine: Engine, visitor: UUID, *, now: datetime | None = None) -> None:
    today = calendar_day(now)
    with engine.begin() as connection:
        connection.exec_driver_sql("SET LOCAL statement_timeout='500ms'")
        _increment(connection, visitor, today, today, hits=1)


def add_review(
    connection: Connection,
    visitor: UUID,
    created_at: datetime,
    *,
    now: datetime | None = None,
) -> None:
    """Call only when a new review is inserted, in that same transaction."""
    _increment(connection, visitor, calendar_day(created_at), calendar_day(now), reviews=1)


def daily_stats(engine: Engine, *, now: datetime | None = None) -> list[DailyStats]:
    today = calendar_day(now)
    start = today - timedelta(days=DAYS - 1)
    with engine.connect() as connection:
        rows = connection.execute(
            text("""SELECT day,sum(hits) hits,count(*) FILTER(WHERE hits>0) uniques,
              sum(reviews) reviews FROM audience_daily
              WHERE day BETWEEN :start AND :today GROUP BY day"""),
            {"start": start, "today": today},
        ).mappings()
        by_day = {
            row["day"]: DailyStats(
                row["day"], int(row["hits"]), row["uniques"], int(row["reviews"])
            )
            for row in rows
        }
    return [
        by_day.get(day, DailyStats(day)) for day in (today - timedelta(days=i) for i in range(DAYS))
    ]
