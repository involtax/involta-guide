"""Read-only USD estimates from saved usage, never budgets or billing records."""

import re
from decimal import Decimal
from typing import Any

PRICING_DATE = "2026-11-08"
PRICING_URL = "https://developers.openai.com/api/docs/pricing"
# Standard processing, USD per million: input, cached input, output.
RATES = {
    "gpt-6-astra": ("10", "1", "50"),
    "gpt-6.1-sol": ("2", "0.10", "10"),
    "gpt-6.1-terra": ("1", "0.05", "5"),
    "gpt-6-luna": ("0.10", "0.01", "0.50"),
    "gpt-5.2": ("1.75", "0.175", "14"),
    "gpt-4.1": ("2", "0.50", "8"),
}


def estimate_cost(request: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    model = response.get("model") or request.get("model") or ""
    family = next(
        (
            name
            for name in RATES
            if model == name or re.fullmatch(re.escape(name) + r"-\d{4}-\d{2}-\d{2}", model)
        ),
        None,
    )
    unavailable = {"available": False, "pricing_date": PRICING_DATE, "pricing_url": PRICING_URL}
    if family is None:
        return {**unavailable, "reason": "Для этой модели тариф не задан."}
    tier = response.get("service_tier") or request.get("service_tier") or "default"
    if tier not in ("default", "standard", "auto"):
        return {**unavailable, "reason": "Тариф этого режима обработки не задан."}
    usage = response.get("usage") or {}
    incoming, outgoing = usage.get("input_tokens"), usage.get("output_tokens")
    details = usage.get("input_tokens_details") or {}
    cached = details.get("cached_tokens", 0)
    if (
        type(incoming) is not int
        or type(outgoing) is not int
        or type(cached) is not int
        or min(incoming, outgoing, cached) < 0
        or cached > incoming
    ):
        return {**unavailable, "reason": "Нет полной статистики токенов."}
    if details.get("cache_write_tokens"):
        return {**unavailable, "reason": "Стоимость записи кеша требует отдельного тарифа."}
    tools = {tool.get("type") for tool in request.get("tools", [])}
    if tools - {"web_search_preview"}:
        return {**unavailable, "reason": "В запросе есть инструменты с неучтённым тарифом."}
    searches = sum(item.get("type") == "web_search_call" for item in response.get("output", []))
    input_rate, cached_rate, output_rate = map(Decimal, RATES[family])
    token_cost = (
        (incoming - cached) * input_rate + cached * cached_rate + outgoing * output_rate
    ) / Decimal(1000000)
    # Preview search: non-reasoning models $25/1k, reasoning models $10/1k.
    search_rate = Decimal("0.025" if family == "gpt-4.1" else "0.010")
    search_cost = searches * search_rate
    total = token_cost + search_cost
    display = "<$0.0001" if 0 < total < Decimal("0.0001") else f"${total:.4f}"
    return {
        "available": True,
        "currency": "USD",
        "amount": str(total),
        "display": display,
        "token_amount": str(token_cost),
        "web_search_amount": str(search_cost),
        "web_search_calls": searches,
        "model": model,
        "pricing_date": PRICING_DATE,
        "pricing_url": PRICING_URL,
        "cache_known": "cached_tokens" in details,
    }
