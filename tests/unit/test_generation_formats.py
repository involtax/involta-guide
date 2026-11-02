import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from involta_guide.application.commands.schemas import Generation, Hours


def test_real_production_response_accepts_object_hours_and_still_rejects_tripadvisor():
    path = Path(__file__).parents[1] / "fixtures/ivanovo-provider-format.json"
    content = json.loads(path.read_text())
    with pytest.raises(ValidationError) as failure:
        Generation.model_validate(content)
    assert [e["loc"] for e in failure.value.errors()] == [
        ("restaurants", 2, "mapsUrl"),
        ("rankingReport", 2, "mapsUrl"),
    ]
    # A bad reserve candidate must not cause hours errors for valid cards.
    content["restaurants"] = content["restaurants"][:2]
    content["rankingReport"] = content["rankingReport"][:2]
    accepted = Generation.model_validate(content).model_dump(mode="json")
    assert accepted["restaurants"][0]["openingHours"]["week"][0] == [["12:00", "00:00"]]


def test_hours_object_intervals_keep_validation_and_support_exceptions():
    value = {
        "timezone": "Europe/Moscow",
        "week": [[{"start": "12:00", "end": "02:00"}]] + [[]] * 6,
        "exceptions": {"2026-11-02": [{"start": "10:00", "end": "15:00"}]},
    }
    assert Hours.model_validate(value).model_dump(mode="json")["exceptions"]["2026-11-02"] == [
        ["10:00", "15:00"]
    ]
    value["week"][0][0]["start"] = "25:00"
    with pytest.raises(ValidationError):
        Hours.model_validate(value)
