from datetime import UTC, date, datetime

import pytest

from involta_stats import DailyStats, calendar_day


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        ("2026-10-31T20:59:59+00:00", date(2026, 10, 31)),
        ("2026-10-31T21:00:00+00:00", date(2026, 11, 1)),
        ("2026-12-31T21:00:00+00:00", date(2027, 1, 1)),
    ],
)
def test_statistics_day_changes_at_midnight_utc_plus_three(instant, expected):
    assert calendar_day(datetime.fromisoformat(instant)) == expected


def test_hits_per_unique_handles_an_empty_day():
    day = datetime.now(UTC).date()
    assert DailyStats(day).hits_per_unique is None
    assert DailyStats(day, hits=7, uniques=2).hits_per_unique == 3.5
