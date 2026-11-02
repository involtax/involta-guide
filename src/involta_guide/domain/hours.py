from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

DAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def opening_status(hours: dict[str, Any] | None, now: datetime | None = None) -> str:
    if not hours:
        return "Режим работы уточняется"
    local = (now or datetime.now(UTC)).astimezone(ZoneInfo(hours["timezone"]))
    minute = local.hour * 60 + local.minute

    def clock(value: str) -> int:
        h, m = value.split(":")
        return int(h) * 60 + int(m)

    def slots(offset: int) -> list[list[str]]:
        day = local.date() + timedelta(days=offset)
        return hours.get("exceptions", {}).get(day.isoformat(), hours["week"][day.weekday()])

    for start, end in slots(-1):
        if clock(end) <= clock(start) and minute < clock(end):
            return "Открыто до " + end
    for start, end in slots(0):
        until = clock(end) + (1440 if clock(end) <= clock(start) else 0)
        if clock(start) <= minute < until:
            return "Открыто до " + end
    for offset in range(14):
        for start, _ in sorted(slots(offset)):
            if offset or clock(start) > minute:
                day = (
                    "сегодня"
                    if offset == 0
                    else "завтра"
                    if offset == 1
                    else (local.date() + timedelta(days=offset)).strftime("%d.%m")
                )
                return f"Откроется {day} в {start}"
    return "Закрыто · уточните расписание"
