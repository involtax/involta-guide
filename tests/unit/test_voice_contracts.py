import ast
from pathlib import Path
from uuid import uuid4

import pytest

from involta_voice.contracts import LocationContext, RestaurantContext, validate_analysis


def test_rating_is_not_invented_from_positive_words_and_fragments_do_not_overlap():
    restaurant = uuid4()
    context = LocationContext(
        id=1,
        slug="ivanovo",
        name="Иваново",
        content_epoch=1,
        restaurants=[RestaurantContext(id=restaurant, name="Огонёк")],
    )
    transcript = "Был в Огоньке сегодня, было вкусно"
    entry = {
        "restaurantId": str(restaurant),
        "quote": transcript,
        "start": 0,
        "end": len(transcript),
        "rating": None,
        "sentiment": 5,
    }
    result = {"outcome": "review", "entries": [entry], "suggestions": []}
    assert validate_analysis(result, transcript, context).entries[0].rating is None
    with pytest.raises(ValueError, match="explicitly"):
        validate_analysis({**result, "entries": [{**entry, "rating": 8}]}, transcript, context)
    with pytest.raises(ValueError, match="Overlapping"):
        validate_analysis({**result, "entries": [entry, entry]}, transcript, context)
    with pytest.raises(ValueError, match="context"):
        validate_analysis(
            {**result, "entries": [{**entry, "restaurantId": str(uuid4())}]}, transcript, context
        )
    with pytest.raises(ValueError, match="Forbidden"):
        validate_analysis(
            {
                **result,
                "suggestions": [
                    {
                        "kind": "edit",
                        "quote": transcript,
                        "reason": "injected",
                        "changes": {"alexStar": True},
                    }
                ],
            },
            transcript,
            context,
        )


def test_explicit_rating_and_unfinished_restaurant_suggestion_are_valid():
    context = LocationContext(id=1, slug="ivanovo", name="Иваново", content_epoch=1)
    transcript = "Оценка 8 из 10, кафе новое"
    result = validate_analysis(
        {
            "outcome": "review",
            "entries": [
                {
                    "quote": transcript,
                    "start": 0,
                    "end": len(transcript),
                    "type": "rating",
                    "rating": 8,
                }
            ],
            "suggestions": [
                {
                    "kind": "restaurant",
                    "name": "Новое",
                    "quote": transcript,
                    "reason": "нужна проверка",
                    "changes": {"phone": None},
                }
            ],
        },
        transcript,
        context,
    )
    assert result.suggestions[0].changes["phone"] is None
    assert result.entries[0].restaurantId is None


def test_voice_package_does_not_import_guide_runtime():
    root = Path(__file__).parents[2] / "src/involta_voice"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(not alias.name.startswith("involta_guide") for alias in node.names), path
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("involta_guide"), path
