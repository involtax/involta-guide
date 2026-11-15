"""Optional maps cannot discard a search; every other contract check stays strict."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from involta_guide.application.commands.schemas import RestaurantInput
from involta_guide.catalogue.schemas import parse_result


def discovery(url):
    return {
        "restaurants": [
            {
                "name": "Тестовый ресторан",
                "mapsUrl": url,
                "sources": ["https://example.test/menu"],
                "reason": "Подтверждено источником",
            }
        ],
        "warnings": [],
    }


@pytest.mark.parametrize(
    "host", ["yandex.ru", "yandex.com", "yandex.com.ge", "yandex.by", "yandex.kz", "yandex.uz"]
)
def test_known_regional_maps_are_preserved(host):
    url = f"https://{host}/maps/org/fixture/123/?lang=ru"
    payload, warnings = parse_result("discover", discovery(url))
    assert payload["restaurants"][0]["mapsUrl"] == url
    assert warnings == []


@pytest.mark.parametrize(
    "url",
    [
        "https://yandex.future-region.test/maps/org/fixture/123/",
        "https://yandex.uz.evil.test/maps/org/fixture/123/",
        "https://fake-yandex.uz/maps/org/fixture/123/",
        "https://yandex.uz@evil.test/maps/org/fixture/123/",
        "https://user:secret@yandex.uz/maps/org/fixture/123/",
        "https://@yandex.uz/maps/org/fixture/123/",
        "https://yandex.uz:broken/maps/org/fixture/123/",
        "https://yandex.uz:8080/maps/org/fixture/123/",
        "https://[malformed/maps/org/fixture/123/",
        "https://yandex.uz\\evil.test/maps/org/fixture/123/",
        "https://yandex.uz/\n/maps/org/fixture/123/",
        "http://yandex.uz/maps/org/fixture/123/",
        "javascript:alert(1)",
        "/maps/org/fixture/123/",
        "https://example.test/restaurant",
        42,
        {},
        [],
    ],
)
def test_bad_or_unknown_map_is_omitted_with_diagnostic_without_mutating_response(url):
    raw = discovery(url)
    raw["restaurants"].append({**raw["restaurants"][0], "name": "Второй", "mapsUrl": None})
    before = deepcopy(raw)
    payload, warnings = parse_result("discover", raw)
    assert len(payload["restaurants"]) == 2
    assert payload["restaurants"][0]["mapsUrl"] is None
    assert warnings[0]["path"] == "restaurants[0].mapsUrl"
    assert len(warnings) == 1
    assert "secret" not in str(warnings)
    assert raw == before
    if isinstance(url, str):
        with pytest.raises(ValueError):
            RestaurantInput.maps(url)  # Strict manual/legacy validation is not relaxed.


def test_internal_map_diagnostics_do_not_overflow_model_warning_limit():
    raw = discovery("https://unknown-map.test/org/1/")
    raw["restaurants"] = [deepcopy(raw["restaurants"][0]) for _ in range(200)]
    raw["warnings"] = [f"Предупреждение модели {i}" for i in range(50)]
    payload, warnings = parse_result("discover", raw)
    assert len(payload["restaurants"]) == len(warnings) == 200
    assert payload["warnings"] == raw["warnings"]
    assert all(row["mapsUrl"] is None for row in payload["restaurants"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", ""),
        ("sources", ["javascript:alert(1)"]),
        ("reason", None),
        ("alexStar", True),
    ],
)
def test_optional_map_fallback_does_not_hide_other_contract_errors(field, value):
    raw = discovery("https://unsupported.test/maps/org/1/")
    raw["restaurants"][0][field] = value
    with pytest.raises(ValidationError):
        parse_result("discover", raw)
