from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import text

from involta_guide.catalogue.schemas import RESULTS
from involta_guide.catalogue.service import CityContent
from involta_guide.domain.entities import Conflict
from involta_guide.domain.rules import FACTORS
from involta_guide.infrastructure.jobs.worker import Worker
from involta_guide.main import create_app

from .test_editorial import CARD
from .test_jobs import setup


class ContentAI:
    model = "fake-no-paid-requests"

    def __init__(self):
        self.calls = []

    def generate(self, prompt, job_id):
        self.calls.append(prompt)
        if prompt.startswith("Операция: intro."):
            return {"intro": "Новый текст о городе."}
        if prompt.startswith("Операция: discover."):
            return {
                "restaurants": [
                    {
                        "name": CARD["name"],
                        "mapsUrl": CARD["mapsUrl"],
                        "sources": ["https://example.com/menu"],
                        "reason": "Ресторан в окрестностях, доступен без проживания",
                    }
                ],
                "warnings": [],
            }
        return research()


def research(value=8, description="Проверенное описание"):
    card = {
        key: value
        for key, value in CARD.items()
        if key not in ("id", "alexStar", "locationId", "locationName")
    }
    card.update(description=description, sources=["https://example.com/menu"])
    return {
        "status": "verified",
        "card": card,
        "assessment": {
            "scores": dict.fromkeys(FACTORS, value),
            "evidence": [
                {
                    "factor": factor,
                    "reason": "Проверено по источникам",
                    "confidence": "medium",
                    "sources": ["https://example.com/menu"],
                }
                for factor in FACTORS
            ],
        },
        "warnings": [],
    }


def imported(
    content, city, operation, result, restaurant=None, scope="all", request_id=None, expected=None
):
    prepared = content.prepare(city, operation, restaurant, scope)
    return content.submit(
        city,
        operation,
        request_id or str(uuid4()),
        expected or prepared["expected"],
        result,
        restaurant,
        scope,
    )


def discovered(database):
    settings, engine, uow, queries, city, jobs, queue = setup(database, ContentAI())
    content = CityContent(uow)
    imported(content, city["id"], "discover", ContentAI().generate("Операция: discover.", ""))
    restaurant = queries.rows("restaurants")[0]
    return settings, engine, uow, queries, city, jobs, queue, content, restaurant


def test_external_imports_are_incremental_and_never_call_or_schedule_model(database):
    settings, engine, uow, queries, city, jobs, queue, content, restaurant = discovered(database)
    assert len(queries.rows("jobs")) == 1
    assert queries.rows("jobs")[0]["status"] == "done"
    assert jobs.provider.calls == []
    imported(content, city["id"], "research", research(), restaurant["id"])
    before = queries.get("restaurants", restaurant["id"])
    imported(content, city["id"], "intro", {"intro": "Карелия и озеро."})
    imported(content, city["id"], "discover", {"restaurants": [], "warnings": []})
    assert queries.get("restaurants", restaurant["id"]) == before
    assert before["active"] == 1
    assert queries.public(city["slug"])["content"]["intro"] == "Карелия и озеро."
    assert len(queries.rows("restaurants")) == 1
    assert not queue.claim()


def test_assessment_card_visibility_and_independent_intro(database):
    _, engine, uow, queries, city, jobs, queue, content, restaurant = discovered(database)
    imported(content, city["id"], "research", research(), restaurant["id"])
    before = queries.get("restaurants", restaurant["id"])
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE restaurants SET alex_star=true WHERE id=:id"), {"id": restaurant["id"]}
        )
    prepared = content.prepare(city["id"], "research", restaurant["id"], "assessment")
    imported(content, city["id"], "intro", {"intro": "Ручной независимый текст"})
    result = research(9)
    result["card"] = None
    imported(
        content,
        city["id"],
        "research",
        result,
        restaurant["id"],
        "assessment",
        expected=prepared["expected"],
    )
    after = queries.get("restaurants", restaurant["id"])
    assert after["description"] == before["description"]
    assert after["ranking"]["scores"]["food"] == 9
    assert after["alex_star"] is True
    content.visibility(restaurant["id"], True)
    imported(content, city["id"], "research", research(10), restaurant["id"])
    assert queries.get("restaurants", restaurant["id"])["active"] == 0
    assert queries.get("restaurants", restaurant["id"])["hidden"] is True
    content.visibility(restaurant["id"], False)
    assert queries.get("restaurants", restaurant["id"])["active"] == 1
    result = research(3)
    result["assessment"] = None
    imported(content, city["id"], "research", result, restaurant["id"], "card")
    assert queries.get("restaurants", restaurant["id"])["ranking"]["scores"]["food"] == 10


def test_idempotency_concurrency_and_stale_block(database):
    _, _, uow, queries, city, jobs, queue = setup(database, ContentAI())
    content = CityContent(uow)
    prepared = content.prepare(city["id"], "intro")
    identity = str(uuid4())

    def submit(_):
        return content.submit(
            city["id"], "intro", identity, prepared["expected"], {"intro": "Первый текст"}
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(submit, range(4)))
    assert len({r["job_id"] for r in results}) == 1
    assert len(queries.rows("jobs")) == 1
    with pytest.raises(Conflict):
        content.submit(city["id"], "intro", identity, prepared["expected"], {"intro": "Подмена"})
    with pytest.raises(Conflict):
        content.submit(
            city["id"], "intro", str(uuid4()), prepared["expected"], {"intro": "Поздний ответ"}
        )
    assert queries.get("locations", city["id"])["intro"] == "Первый текст"


def test_queued_pipeline_retains_prompt_and_publishes_after_restaurant_research(database):
    _, _, uow, queries, city, jobs, queue = setup(database, ContentAI())
    state = jobs.enqueue_public_fill(city["id"])
    assert state["state"] == "queued"
    assert len(queries.rows("jobs")) == 2
    worker = Worker(queue, jobs)
    assert worker.once()  # intro
    assert worker.once()  # discovery -> research
    assert jobs.public_fill_state(city["id"])["state"] in ("queued", "running")
    assert worker.once()
    assert jobs.public_fill_state(city["id"])["state"] == "ready"
    assert queries.get("locations", city["id"])["initialized_at"]
    assert len(queries.rows("restaurants")) == 1
    assert all(j["applied"] for j in queries.rows("jobs"))
    before = queries.rows("restaurants")[0]
    CityContent(uow).enqueue(city["id"], "discover")
    worker.once()
    assert not queue.claim()  # A complete existing card is not researched again.
    assert queries.rows("restaurants")[0] == before


def test_stale_worker_does_not_overwrite_intro_and_releases_lease(database):
    _, _, uow, queries, city, jobs, queue = setup(database, ContentAI())
    content = CityContent(uow)
    content.enqueue(city["id"], "intro")
    job = queue.claim()
    imported(content, city["id"], "intro", {"intro": "Администратор поправил"})
    with pytest.raises(Conflict):
        jobs.process(job)
    assert queries.get("locations", city["id"])["intro"] == "Администратор поправил"


def test_private_api_auth_context_import_run_and_images_untouched(database):
    settings, engine, uow, queries, city, jobs, queue = setup(database, ContentAI())
    token = "private-api-tests-" + "x" * 40
    settings.agent_api_token = SecretStr(token)
    headers = {"Authorization": "Bearer " + token}
    base = f"/guide/api/admin/locations/{city['id']}/intro"
    with TestClient(create_app(settings)) as client:
        assert client.get(base + "/context").status_code == 401
        assert client.post(base + "/run", json={"request_id": str(uuid4())}).status_code == 401
        response = client.get(base + "/context", headers=headers)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        prepared = response.json()
        assert "schema" in prepared and "prompt" in prepared
        assert queries.rows("jobs") == []
        body = {
            "request_id": str(uuid4()),
            "expected": prepared["expected"],
            "result": {"intro": "Внешний текст"},
        }
        assert client.post(base + "/result", json=body, headers=headers).status_code == 200
        assert client.post(base + "/result", json=body, headers=headers).json()["applied"] is True
        assert len(queries.rows("jobs")) == 1
        assert (
            client.post(
                base + "/result", json={**body, "request_id": str(uuid4())}, headers=headers
            ).status_code
            == 409
        )
        invalid = {**body, "request_id": str(uuid4()), "images": []}
        assert client.post(base + "/result", json=invalid, headers=headers).status_code == 400
        assert client.post(base + "/clear", json={}, headers=headers).status_code in (404, 405)
        assert (
            client.post(
                base + "/result", content="x" * (1024 * 1024 + 1), headers=headers
            ).status_code
            == 413
        )
        run = {"request_id": str(uuid4())}
        first = client.post(base + "/run", json=run, headers=headers)
        assert first.status_code == 202
        assert client.post(base + "/run", json=run, headers=headers).json() == first.json()
        assert len(queries.rows("jobs")) == 2
        assert (
            client.get("/guide/api/admin/jobs/" + first.json()["job_id"], headers=headers).json()[
                "status"
            ]
            == "queued"
        )
        settings.agent_api_token = SecretStr("")
        assert client.get(base + "/context", headers=headers).status_code == 401
    assert jobs.provider.calls == []


@pytest.mark.parametrize(
    "operation,payload",
    [
        ("intro", {"intro": "   "}),
        (
            "discover",
            {"restaurants": [{"name": "X", "sources": ["http://bad.test"], "reason": "X"}]},
        ),
        ("research", {**research(), "alexStar": True}),
    ],
)
def test_schema_rejects_untrusted_or_out_of_scope_fields(operation, payload):
    with pytest.raises(ValidationError):
        RESULTS[operation].model_validate(payload)


def test_discovery_without_map_stays_in_catalogue_and_bad_scope_is_atomic(database):
    _, _, uow, queries, city, _, queue = setup(database, ContentAI())
    content = CityContent(uow)
    imported(
        content,
        city["id"],
        "discover",
        {
            "restaurants": [
                {
                    "name": "Ресторан на озере",
                    "mapsUrl": None,
                    "sources": ["https://example.com/menu"],
                    "reason": "Загородное место",
                }
            ],
            "warnings": [],
        },
    )
    restaurant = queries.rows("restaurants")[0]
    assert restaurant["active"] == 0 and restaurant["maps_url"] == ""
    before_jobs = queries.rows("jobs")
    from involta_guide.domain.entities import RuleViolation

    with pytest.raises(RuleViolation):
        imported(content, city["id"], "research", research(), restaurant["id"], "assessment")
    assert queries.rows("jobs") == before_jobs
    assert queries.rows("restaurants") == [restaurant]
    assert queue.claim() is None


def test_migration_keeps_existing_content_and_active_state(database):
    from alembic import command
    from alembic.config import Config

    settings, engine, uow, queries, city, _, _ = setup(database, ContentAI())
    config = Config(str(settings.root_dir / "alembic.ini"))
    command.downgrade(config, "vip_visitors")
    with engine.begin() as conn:
        for active in (0, 1):
            conn.execute(
                text(
                    "INSERT INTO restaurants(id,location_id,name,active,alex_star) VALUES(gen_random_uuid(),:id,:name,:active,true)"
                ),
                {"id": city["id"], "name": "Старое место " + str(active), "active": active},
            )
        before = [
            dict(row)
            for row in conn.execute(text("SELECT * FROM restaurants ORDER BY number")).mappings()
        ]
    command.upgrade(config, "head")
    with engine.connect() as conn:
        after = [
            dict(row)
            for row in conn.execute(text("SELECT * FROM restaurants ORDER BY number")).mappings()
        ]
    for old, new in zip(before, after, strict=True):
        assert {key: new[key] for key in old} == old
        assert new["hidden"] is False
        assert new["verification_status"] == ("verified" if old["active"] else "needs_verification")


def test_queued_intro_uses_frozen_prompt_and_model_without_web_search(database):
    import json

    import httpx

    from involta_guide.application.services.jobs import JobService
    from involta_guide.infrastructure.ai.openai import OpenAIProvider

    settings, engine, uow, queries, city, _, queue = setup(database, ContentAI())
    settings.openai_api_key = SecretStr("test-mock-only")
    content = CityContent(uow)
    first_prompts = {op: "Первый промпт. " * 8 for op in RESULTS}
    content.save_settings(first_prompts, "gpt-5")
    identity = content.enqueue(city["id"], "intro")
    content.save_settings({op: "Второй промпт. " * 8 for op in RESULTS}, "gpt-4.1")
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "resp_new_intro",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps({"intro": "Вступление из нового контракта"}),
                            }
                        ],
                    }
                ],
            },
        )

    provider = OpenAIProvider(settings, engine)
    provider.client.close()
    provider.client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        assert Worker(queue, JobService(uow, provider)).once()
        assert queries.get("jobs", identity)["applied"]
        assert captured[0]["model"] == "gpt-5"
        assert captured[0]["input"][1]["content"].startswith(first_prompts["intro"])
        assert captured[0]["input"][0]["content"] == content.prepare(city["id"], "intro")["system"]
        assert "tools" not in captured[0]
        assert captured[0]["text"]["format"]["name"] == "content_intro"
    finally:
        provider.close()


def test_show_first_hidden_researched_place_completes_initial_publication(database):
    _, _, _, queries, city, _, _, content, restaurant = discovered(database)
    content.visibility(restaurant["id"], True)
    imported(content, city["id"], "research", research(), restaurant["id"])
    assert queries.get("locations", city["id"])["initialized_at"] is None
    content.visibility(restaurant["id"], False)
    assert queries.get("restaurants", restaurant["id"])["active"] == 1
    assert queries.get("locations", city["id"])["initialized_at"] is not None
