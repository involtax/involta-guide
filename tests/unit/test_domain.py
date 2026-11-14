import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest

from involta_guide.domain.entities import Conflict, RuleViolation
from involta_guide.domain.hours import opening_status
from involta_guide.domain.rules import (
    FACTORS,
    assert_proposal,
    assert_publishable,
    assert_revision,
    rank,
    score,
    similarity,
    transition_job,
)


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([9, 8, 9, 8, 9, 8, 7, 9], 8.38),
        ([8, 8, 8, 6, 9, 8, 7, 8], 7.75),
        ([8, 8, 7, 7, 8, 8, 7, 8], 7.63),
        ([8, 7, 8, 5, 8, 8, 7, 8], 7.38),
        ([8, 7, 6, 3, 8, 7, 8, 7], 6.75),
        ([8, 7, 6, 3, 7, 7, 8, 7], 6.63),
    ],
)
def test_equal_weights(values, expected):
    assert score(dict(zip(FACTORS, values))) == expected


def test_unknown_scores_and_order():
    assert score(dict(zip(FACTORS, [8] * 7 + [None]))) is None
    report = [
        {"name": "Б", "mapsUrl": "https://yandex.ru/maps/2", "scores": dict.fromkeys(FACTORS, 7)},
        {"name": "А", "mapsUrl": "https://yandex.ru/maps/1", "scores": dict.fromkeys(FACTORS, 9)},
    ]
    result = rank({"rankingReport": report, "restaurants": [{"name": "Б"}, {"name": "А"}]}, 1)
    assert [row["name"] for row in result["restaurants"]] == ["А", "Б"]


def test_transitions_revision_and_fraud_rules():
    with pytest.raises(Conflict):
        assert_revision("new", "old")
    with pytest.raises(RuleViolation):
        assert_proposal("approved", "approved", False)
    with pytest.raises(RuleViolation):
        assert_proposal("pending", "approved", True)
    with pytest.raises(RuleViolation):
        transition_job("done", "running")
    transition_job("queued", "running")
    with pytest.raises(RuleViolation):
        assert_publishable({"restaurants": []})
    assert similarity("Плохой сервис холодный суп", "Плохой сервис холодный суп") == 1
    assert similarity("Суп", "Стейк") == 0


def test_hours_timezone_night_and_exception():
    hours = {
        "timezone": "Europe/Moscow",
        "week": [[["12:00", "02:00"]], [], [], [], [["12:00", "23:00"]], [], []],
    }
    assert opening_status(hours, datetime(2026, 11, 2, 22, tzinfo=UTC)) == "Открыто до 02:00"
    assert opening_status(hours, datetime(2026, 11, 2, 23, tzinfo=UTC)) == "Откроется 06.11 в 12:00"
    hours["exceptions"] = {"2026-11-2": [], "2026-11-02": [["15:00", "18:00"], ["19:00", "22:00"]]}
    assert (
        opening_status(hours, datetime(2026, 11, 2, 15, 30, tzinfo=UTC))
        == "Откроется сегодня в 19:00"
    )


def test_domain_has_no_external_imports():
    root = Path(__file__).resolve().parents[2] / "src/involta_guide/domain"
    forbidden = (
        "fastapi",
        "sqlalchemy",
        "jinja2",
        "pydantic",
        "httpx",
        "openai",
        "involta_guide.infrastructure",
        "involta_guide.web",
    )
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            modules = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [name.name for name in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            assert not any(module.startswith(forbidden) for module in modules)


def test_external_urls_and_numeric_review_scales():
    from pydantic import ValidationError

    from involta_guide.application.commands.schemas import RestaurantInput
    from involta_voice.contracts import ReviewFragment

    with pytest.raises(ValidationError):
        RestaurantInput(
            name="X", cuisine="X", description="X", mapsUrl="https://yandex.ru.evil.test/"
        )
    with pytest.raises(ValidationError):
        RestaurantInput(name="X", cuisine="X", description="X", mapsUrl="javascript:alert(1)")
    entry = {
        "restaurantId": None,
        "restaurantName": "X",
        "type": "rating",
        "text": None,
        "quote": "X",
        "sentiment": None,
        "recommendation": None,
        "rating": None,
        "confidence": 0.5,
        "topics": [],
        "experience": "unknown",
    }
    entry.update(start=0, end=1, sentiment=-10, recommendation=10, rating=0)
    assert ReviewFragment.model_validate(entry).rating == 0
    with pytest.raises(ValidationError):
        ReviewFragment.model_validate({**entry, "rating": 11})


def test_regional_yandex_maps_url_is_kept_intact():
    from involta_guide.catalogue.schemas import Card, Discovery

    url = "https://yandex.com.ge/maps/org/roscha_vikonda/97317027367/"
    fields = {"name": "Роща", "mapsUrl": url, "sources": [url]}
    assert Discovery(**fields, reason="Ресторан в Рыбинске").mapsUrl == url
    assert Card(**fields).mapsUrl == url


@pytest.mark.parametrize(
    "url",
    [
        "https://yandex.com.ge.evil.test/maps/org/1/",
        "https://fake-yandex.com.ge/maps/org/1/",
        "https://yandex.com.ge@evil.test/maps/org/1/",
        "https://user:password@yandex.com.ge/maps/org/1/",
        "http://yandex.com.ge/maps/org/1/",
    ],
)
def test_regional_map_support_keeps_rejecting_unsafe_urls(url):
    from involta_guide.application.commands.schemas import RestaurantInput

    with pytest.raises(ValueError, match="Нужна HTTPS-ссылка на Яндекс Карты"):
        RestaurantInput.maps(url)
