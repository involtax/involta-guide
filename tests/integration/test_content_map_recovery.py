from copy import deepcopy
from uuid import uuid4

import pytest

from involta_guide.application.commands.schemas import RestaurantInput
from involta_guide.domain.entities import Conflict

from .test_incremental_content import discovered, imported, research


@pytest.mark.parametrize("known_map", [True, False])
@pytest.mark.parametrize("hidden", [True, False])
def test_research_keeps_known_map_or_saves_unpublished_facts(database, known_map, hidden):
    _, _, uow, queries, city, _, _, content, restaurant = discovered(database)
    if known_map:
        imported(content, city["id"], "research", research(), restaurant["id"])
    else:
        with uow() as work:
            work.repo.change("restaurants", restaurant["id"], {"maps_url": ""})
    content.visibility(restaurant["id"], hidden)
    before = queries.get("restaurants", restaurant["id"])
    raw = research(9, "Новые подтверждённые факты")
    raw["card"]["mapsUrl"] = "https://yandex.future-region.test/maps/org/fixture/1/"
    original = deepcopy(raw)
    result = imported(content, city["id"], "research", raw, restaurant["id"])
    after = queries.get("restaurants", restaurant["id"])
    job = queries.get("jobs", result["job_id"])
    assert job["status"] == "done" and job["applied"]
    assert after["maps_url"] == before["maps_url"]
    assert after["description"] == "Новые подтверждённые факты"
    assert after["hidden"] is hidden
    assert after["active"] == int(known_map and not hidden)
    assert job["report"]["map_warnings"][0]["path"] == "card.mapsUrl"
    assert raw == original
    if known_map:
        assert after["ranking"]["scores"]["food"] == 9
        assert len(job["report"]["map_warnings"]) == 1
    else:
        assert after["ranking"] is None
        assert after["verification_status"] == "needs_verification"
        assert job["report"]["map_warnings"][1]["path"] == "assessment"
        assert queries.public(city["slug"])["content"]["restaurants"] == []


def test_import_retry_does_not_ignore_different_rejected_url_or_erase_known_map(
    database, monkeypatch
):
    _, _, _, queries, city, _, _, content, restaurant = discovered(database)
    raw = {
        "restaurants": [
            {
                "name": restaurant["name"],
                "mapsUrl": "https://unsupported.test/first/",
                "sources": ["https://example.test/new-menu"],
                "reason": "Новый источник",
            }
        ],
        "warnings": [],
    }
    request_id = str(uuid4())
    context = content.prepare(city["id"], "discover")
    first = imported(
        content, city["id"], "discover", raw, request_id=request_id, expected=context["expected"]
    )
    assert (
        imported(
            content,
            city["id"],
            "discover",
            raw,
            request_id=request_id,
            expected=context["expected"],
        )
        == first
    )
    # A future release accepts this host: replay must still return the original result.
    with monkeypatch.context() as patch:
        patch.setattr(RestaurantInput, "maps", lambda value: value)
        assert (
            imported(
                content,
                city["id"],
                "discover",
                raw,
                request_id=request_id,
                expected=context["expected"],
            )
            == first
        )
    raw["restaurants"][0]["mapsUrl"] = "https://unsupported.test/different/"
    with pytest.raises(Conflict):
        imported(
            content,
            city["id"],
            "discover",
            raw,
            request_id=request_id,
            expected=context["expected"],
        )
    rows = queries.rows("restaurants")
    assert len(rows) == 1 and rows[0]["maps_url"] == restaurant["maps_url"]
    assert rows[0]["sources"] == [*restaurant["sources"], "https://example.test/new-menu"]
