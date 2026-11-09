"""Assessments require an actual restaurant card in admin, API and queued result application."""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from involta_guide.domain.entities import Conflict, RuleViolation
from involta_guide.infrastructure.jobs.worker import Worker
from involta_guide.main import create_app

from .test_incremental_content import discovered, imported, research
from .test_web import login


def test_placeholder_assessment_is_rejected_before_queue_or_model(database):
    settings, _, _, queries, city, jobs, queue, content, restaurant = discovered(database)
    token = "assessment-test-" + "x" * 40
    settings.agent_api_token = SecretStr(token)
    headers = {"Authorization": "Bearer " + token}
    base = f"/guide/api/admin/locations/{city['id']}/research"
    params = {"restaurant_id": restaurant["id"], "scope": "assessment"}
    before = queries.rows("jobs")
    with TestClient(create_app(settings)) as client:
        response = client.get(base + "/context", params=params, headers=headers)
        assert response.status_code == 409 and "scope=all" in response.json()["error"]
        response = client.post(
            base + "/run", params=params, headers=headers, json={"request_id": str(uuid4())}
        )
        assert response.status_code == 409
        # Even a client attempting to skip GET context cannot import a detached assessment.
        result = research()
        result["card"] = None
        response = client.post(
            base + "/result",
            params=params,
            headers=headers,
            json={"request_id": str(uuid4()), "expected": "0" * 64, "result": result},
        )
        assert response.status_code == 409
        assert (
            client.get(f"/guide/api/admin/locations/{city['id']}", headers=headers).json()[
                "restaurants"
            ][0]["card_complete"]
            is False
        )
    assert queries.rows("jobs") == before and jobs.provider.calls == []
    assert queue.claim() is None
    assert queries.get("restaurants", restaurant["id"])["ranking"] is None


def test_full_first_research_then_independent_reassessment(database):
    _, _, _, queries, city, jobs, queue, content, restaurant = discovered(database)
    identity = content.enqueue(city["id"], "research", restaurant["id"], "all")
    assert Worker(queue, jobs).once()
    assert queries.get("jobs", identity)["applied"]
    before = queries.get("restaurants", restaurant["id"])
    assert before["active"] == 1
    result = research(9)
    result["card"] = None
    imported(content, city["id"], "research", result, restaurant["id"], "assessment")
    after = queries.get("restaurants", restaurant["id"])
    assert after["ranking"]["scores"]["food"] == 9
    for field in (
        "id",
        "name",
        "description",
        "phone",
        "maps_url",
        "sources",
        "alex_star",
        "hidden",
    ):
        assert after[field] == before[field]


def test_import_cannot_save_score_and_incomplete_card_as_a_verified_result(database):
    _, _, _, queries, city, _, _, content, restaurant = discovered(database)
    before = queries.get("restaurants", restaurant["id"])
    before_jobs = queries.rows("jobs")
    result = research()
    result["card"]["phone"] = None
    with pytest.raises(RuleViolation, match="Оценка не сохранена"):
        imported(content, city["id"], "research", result, restaurant["id"])
    assert queries.get("restaurants", restaurant["id"]) == before
    assert queries.rows("jobs") == before_jobs


def test_partial_research_keeps_found_facts_without_a_score(database):
    _, _, _, queries, city, _, _, content, restaurant = discovered(database)
    result = research()
    result.update(status="needs_verification", assessment=None)
    result["card"]["phone"] = None
    imported(content, city["id"], "research", result, restaurant["id"])
    after = queries.get("restaurants", restaurant["id"])
    assert after["description"] == result["card"]["description"]
    assert after["ranking"] is None and after["active"] == 0
    with pytest.raises(Conflict):
        content.prepare(city["id"], "research", restaurant["id"], "assessment")


def test_old_scores_survive_card_completion_without_a_new_assessment(database):
    _, _, uow, queries, city, _, _, content, restaurant = discovered(database)
    old_ranking = research(7)["assessment"]
    with uow() as work:
        work.repo.change("restaurants", restaurant["id"], {"ranking": old_ranking})
    with pytest.raises(Conflict):
        content.prepare(city["id"], "research", restaurant["id"], "assessment")
    result = research()
    result["assessment"] = None
    imported(content, city["id"], "research", result, restaurant["id"], "card")
    after = queries.get("restaurants", restaurant["id"])
    assert after["ranking"] == old_ranking and after["active"] == 1


def test_replay_returns_original_job_even_if_card_later_becomes_incomplete(database):
    _, _, uow, queries, city, _, _, content, restaurant = discovered(database)
    imported(content, city["id"], "research", research(), restaurant["id"])
    request_id = str(uuid4())
    identity = content.enqueue(city["id"], "research", restaurant["id"], "assessment", request_id)
    with uow() as work:
        work.repo.change("restaurants", restaurant["id"], {"phone": ""})
    assert (
        content.enqueue(city["id"], "research", restaurant["id"], "assessment", request_id)
        == identity
    )
    with pytest.raises(Conflict):
        content.enqueue(city["id"], "research", restaurant["id"], "assessment", str(uuid4()))
    assert len([j for j in queries.rows("jobs") if j["status"] == "queued"]) == 1


def test_admin_list_and_detail_offer_only_first_research_for_a_placeholder(database):
    settings, _, _, queries, city, _, _, content, restaurant = discovered(database)
    with TestClient(create_app(settings)) as client:
        csrf = login(client)
        for path in (
            "/guide/admin/restaurants/" + city["slug"],
            "/guide/admin/restaurants/" + str(restaurant["number"]),
        ):
            page = client.get(path)
            assert "Заполнить и оценить" in page.text
            assert "/content/assessment" not in page.text and "/content/card" not in page.text
        response = client.post(
            f"/guide/admin/restaurants/{restaurant['number']}/content/assessment",
            data={"_csrf": csrf},
            headers={"origin": "http://testserver"},
        )
        assert response.status_code == 409
        result = research()
        result["assessment"] = None
        imported(content, city["id"], "research", result, restaurant["id"], "card")
        page = client.get("/guide/admin/restaurants/" + city["slug"])
        assert ">Оценить</button>" in page.text and "/content/card" in page.text
        assert "Заполнить и оценить" not in page.text


@pytest.mark.parametrize(
    "field,value",
    [
        ("description", ""),
        ("maps_url", ""),
        ("phone", ""),
        ("sources", []),
        ("description", "   "),
        ("phone", "   "),
    ],
)
def test_existing_score_does_not_allow_reassessment_with_a_missing_card_field(
    database, field, value
):
    _, _, uow, queries, city, _, _, content, restaurant = discovered(database)
    imported(content, city["id"], "research", research(), restaurant["id"])
    with uow() as work:
        work.repo.change("restaurants", restaurant["id"], {field: value})
    before = queries.get("restaurants", restaurant["id"])
    with pytest.raises(Conflict):
        content.enqueue(city["id"], "research", restaurant["id"], "assessment")
    assert queries.get("restaurants", restaurant["id"]) == before


def test_old_queued_assessment_cannot_apply_after_card_fields_are_removed(database):
    _, _, uow, queries, city, jobs, queue, content, restaurant = discovered(database)
    imported(content, city["id"], "research", research(), restaurant["id"])
    identity = content.enqueue(city["id"], "research", restaurant["id"], "assessment")
    with uow() as work:
        work.repo.change("restaurants", restaurant["id"], {"phone": ""})
    before = queries.get("restaurants", restaurant["id"])
    assert Worker(queue, jobs).once()
    assert queries.get("jobs", identity)["status"] == "failed"
    assert queries.get("restaurants", restaurant["id"]) == before
