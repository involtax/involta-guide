"""Supported research models, without monetary accounting."""

MODELS_URL = "https://developers.openai.com/api/docs/models"

GENERATION_MODELS = (
    {
        "id": "gpt-6.1-sol",
        "name": "GPT-6.1 Sol",
        "purpose": "Основная модель для наполнения",
        "recommended": True,
    },
    {
        "id": "gpt-6-astra",
        "name": "GPT-6 Astra",
        "purpose": "Для сложного исследования и проверки",
        "recommended": False,
    },
    {
        "id": "gpt-6.1-terra",
        "name": "GPT-6.1 Terra",
        "purpose": "Универсальная альтернатива",
        "recommended": False,
    },
    {
        "id": "gpt-6-luna",
        "name": "GPT-6 Luna",
        "purpose": "Для простых исследовательских задач",
        "recommended": False,
    },
    {
        "id": "gpt-5.2",
        "name": "GPT-5.2",
        "purpose": "Предыдущее поколение для сравнения",
        "recommended": False,
    },
    {"id": "gpt-4.1", "name": "GPT-4.1", "purpose": "Прежняя базовая модель", "recommended": False},
)
GENERATION_MODEL_IDS = frozenset(str(m["id"]) for m in GENERATION_MODELS)
