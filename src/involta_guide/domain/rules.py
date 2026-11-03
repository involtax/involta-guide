import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .entities import Conflict, RuleViolation

FACTORS = (
    "food",
    "consistency",
    "identity",
    "localConnection",
    "atmosphere",
    "service",
    "value",
    "destination",
)
FACTOR_LABELS = (
    "Еда",
    "Стабильность",
    "Самобытность",
    "Связь с местом",
    "Атмосфера",
    "Сервис",
    "Цена / результат",
    "Ради визита",
)
KINDS = {
    "city": "Город",
    "settlement": "Населённый пункт",
    "district": "Район",
    "neighbourhood": "Городская зона",
    "resort": "Курорт",
    "tourist_area": "Туристическая территория",
    "federal_territory": "Федеральная территория",
    "destination": "Направление",
}


def score(values: dict[str, int | None]) -> float | None:
    items = [values.get(key) for key in FACTORS]
    if any(type(value) is not int or not 0 <= value <= 10 for value in items):
        return None
    return float(
        (Decimal(sum(value for value in items if value is not None)) / 8).quantize(
            Decimal(".01"), rounding=ROUND_HALF_UP
        )
    )


def rank(content: dict[str, Any], limit: int) -> dict[str, Any]:
    report = content.get("rankingReport", [])

    def key(s: str) -> str:
        return " ".join(s.casefold().split())

    if len({key(row["name"]) for row in report}) != len(report):
        raise RuleViolation("ИИ вернул дубликаты кандидатов")
    for row in report:
        row["totalScore"] = score(row["scores"])
    report.sort(
        key=lambda row: (
            -(row["totalScore"] if row["totalScore"] is not None else -1),
            key(row["name"]),
        )
    )
    cards = {key(row["name"]): row for row in content["restaurants"]}
    eligible = [row for row in report if row["totalScore"] is not None and row.get("mapsUrl")]
    result = []
    for index, row in enumerate(eligible):
        card = cards.get(key(row["name"]))
        if card is None:
            if index < limit:
                raise RuleViolation("Нет карточки лидера рейтинга")
            continue
        result.append({**card, "mapsUrl": row["mapsUrl"]})
    if len(result) != len(content["restaurants"]):
        raise RuleViolation("В подборке есть ресторан вне рассчитанного топа")
    return {**content, "restaurants": result, "rankingReport": report}


def assert_revision(actual: str | None, expected: str | None) -> None:
    if actual != expected:
        raise Conflict("Страница изменена. Обновите редактор, чтобы не потерять ручные правки.")


def assert_publishable(content: dict[str, Any]) -> None:
    cards = content["restaurants"]
    if not cards:
        raise RuleViolation("Нельзя опубликовать пустую подборку")
    missing = [r["name"] for r in cards if not re.fullmatch(r"\+[1-9]\d{7,14}", r.get("phone", ""))]
    if missing:
        raise RuleViolation("Для публикации нужен телефон: " + ", ".join(missing))


def assert_proposal(status: str, moderation: str, fraud: bool) -> None:
    if status != "pending":
        raise Conflict("Предложение уже обработано")
    if moderation != "approved" or fraud:
        raise RuleViolation("Сначала одобрите отзыв и снимите отметку фрода")


def transition_job(current: str, target: str) -> None:
    allowed = {
        "queued": {"running", "cancelled"},
        "running": {"queued", "done", "failed"},
        "failed": {"queued"},
        "done": set(),
        "cancelled": set(),
    }
    if target not in allowed.get(current, set()):
        raise RuleViolation(f"Недопустимый переход задания: {current} -> {target}")


def similarity(a: str | None, b: str | None) -> float:
    def words(text: str | None) -> set[str]:
        return {word for word in re.findall(r"\w+", (text or "").lower()) if len(word) > 2}

    left, right = words(a), words(b)
    return len(left & right) / len(left | right) if left and right else 0.0


def aggregate_scores(reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, Any] = {}
    for review in reviews:
        if review["moderation"] != "approved" or review["fraud"]:
            continue
        for entry in (review["result"] or {}).get("entries", []):
            key = entry.get("restaurantId") or entry.get("restaurantName")
            if not key:
                continue
            group = groups.setdefault(
                key,
                {
                    "name": entry["restaurantName"],
                    "visitors": set(),
                    "rating": [],
                    "sentiment": [],
                    "recommendation": [],
                },
            )
            group["visitors"].add(review["visitor"])
            for attr in ("rating", "sentiment", "recommendation"):
                if entry.get(attr) is not None:
                    group[attr].append(entry[attr])
    return [
        {
            "name": g["name"],
            "visitors": len(g["visitors"]),
            **{
                key: sum(g[key]) / len(g[key]) if g[key] else None
                for key in ("rating", "sentiment", "recommendation")
            },
        }
        for g in groups.values()
    ]
