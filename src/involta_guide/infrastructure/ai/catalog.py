"""Curated Responses + web-search models; official Standard prices in USD."""

from decimal import ROUND_HALF_UP, Decimal

PRICES_CHECKED = "02.11.2026"
PRICING_URL = "https://developers.openai.com/api/docs/pricing"
MODELS_URL = "https://developers.openai.com/api/docs/models"


def _model(
    identifier: str,
    name: str,
    purpose: str,
    input_price: str,
    output_price: str,
    search_price: str = "10",
    *,
    recommended: bool = False,
) -> dict[str, str | bool]:
    # Comparison scenario, not a prediction: 10K input + 5K output + 5 searches.
    example = Decimal(input_price) / 100 + Decimal(output_price) / 200 + Decimal(search_price) / 200
    return {
        "id": identifier,
        "name": name,
        "purpose": purpose,
        "input_price": input_price,
        "output_price": output_price,
        "search_price": search_price,
        "example_price": str(example.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        "recommended": recommended,
    }


GENERATION_MODELS = (
    _model(
        "gpt-6.1-sol",
        "GPT-6.1 Sol",
        "Баланс качества и цены — рекомендую для наполнения",
        "2",
        "10",
        recommended=True,
    ),
    _model(
        "gpt-6-astra",
        "GPT-6 Astra",
        "Максимальное качество для сложных подборок; дороже",
        "10",
        "50",
    ),
    _model("gpt-6.1-terra", "GPT-6.1 Terra", "Экономный универсальный вариант", "1", "5"),
    _model("gpt-6-luna", "GPT-6 Luna", "Самый дешёвый вариант для черновиков", "0.10", "0.50"),
    _model("gpt-5.2", "GPT-5.2", "Предыдущее поколение для сравнения", "1.75", "14"),
    # The current integration uses web_search_preview; non-reasoning search costs $25/1K.
    _model("gpt-4.1", "GPT-4.1", "Прежняя базовая модель", "2", "8", "25"),
)
GENERATION_MODEL_IDS = frozenset(str(m["id"]) for m in GENERATION_MODELS)
