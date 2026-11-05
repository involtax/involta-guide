import json
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_guide.infrastructure.ai.audit import AIAudit
from involta_guide.infrastructure.jobs.worker import Worker
from involta_guide.main import create_app

from .test_background_generation import configured, envelope, task
from .test_jobs import provider_payload, setup


def shortlist(count=10):
    raw = provider_payload()
    original = raw["candidates"][0]
    raw["candidates"] = []
    for index in range(count):
        candidate = deepcopy(original)
        candidate.update(
            name=f"Кандидат {index:02}",
            mapsUrl=f"https://yandex.ru/maps/org/place/{1000 + index}/",
            openingHours=None,
            latitude=None,
            longitude=None,
        )
        candidate["scores"] = dict.fromkeys(candidate["scores"], min(9, index))
        raw["candidates"].append(candidate)
    return raw


@pytest.mark.parametrize("blocked_leader", [False, True])
def test_ten_candidates_produce_three_catalogue_winners_and_a_full_audit(database, blocked_leader):
    raw = shortlist()
    raw["discoveredRestaurants"] = [
        {"name": "Новая кофейня", "sources": []},
        {"name": "  новая   кофейня ", "sources": []},
    ]
    # Equal leaders arrive in reverse alphabetical order; the server owns the ranking.
    raw["candidates"][8]["scores"] = deepcopy(raw["candidates"][9]["scores"])
    if blocked_leader:
        raw["candidates"][9].update(
            status="needs_verification",
            verificationIssues=["location"],
            reason="Не разрешено противоречие адресов филиала",
        )
    raw["candidates"].reverse()
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        response = envelope()
        response["output"][0]["content"][0]["text"] = json.dumps(raw)
        return httpx.Response(200, json=response)

    settings, engine, queries, location, service, queue, provider, job = configured(
        database, handler
    )
    try:
        assert Worker(queue, service).once()
        stored = queries.get("jobs", job["id"])
        assert stored["status"] == "done" and stored["applied"]
        winners = queries.location(location["id"])["content"]["restaurants"]
        expected = (
            ["Кандидат 08", "Кандидат 07", "Кандидат 06"]
            if blocked_leader
            else ["Кандидат 08", "Кандидат 09", "Кандидат 07"]
        )
        assert [r["name"] for r in winners] == expected
        assert len(queries.rows("restaurants")) == (10 if blocked_leader else 11)
        extra = next(r for r in queries.rows("restaurants") if r["name"] == "Новая кофейня")
        assert extra["active"] == 0 and extra["phone"] is None and extra["ranking"] is None
        assert extra["description"] is None
        report = stored["report"]
        assert len(report["rankingReport"]) == len(report["candidateReview"]) == 10
        assert report["qualityReport"]["verified"] == (9 if blocked_leader else 10)
        assert report["qualityReport"]["publishedCards"] == 3
        assert report["qualityReport"]["status"] == ("partial" if blocked_leader else "complete")
        assert all(r["openingHours"] is None for r in winners)
        saved = task(engine, job)
        assert json.loads(saved["response_json"]["output"][0]["content"][0]["text"]) == raw
        assert len(requests) == 1
        schema = requests[0]["text"]["format"]
        assert schema["name"] == "generation_v4"
        assert schema["schema"]["properties"]["candidates"]["maxItems"] == 10
        assert '"publishedLimit": 3' in requests[0]["input"][1]["content"]
        with TestClient(create_app(settings)) as client:
            html = client.get("/guide/" + location["slug"]).text
            assert html.count('<article class="restaurant">') == 3
            assert 'class="more-restaurants"' not in html
            exported = client.get("/guide/logs/" + location["slug"] + "/1.json").json()
            assert len(exported["job"]["report"]["candidateReview"]) == 10
    finally:
        provider.close()


@pytest.mark.parametrize("failure", ["eleventh_candidate", "optional_hours_blocker"])
def test_contract_violation_is_saved_without_publication_or_another_post(database, failure):
    raw = shortlist(11 if failure == "eleventh_candidate" else 1)
    if failure == "optional_hours_blocker":
        raw["candidates"][0].update(
            status="needs_verification",
            reason="Не удалось найти часы работы",
            verificationIssues=["hours"],
        )
    methods = []

    def handler(request):
        methods.append(request.method)
        response = envelope()
        response["output"][0]["content"][0]["text"] = json.dumps(raw)
        return httpx.Response(200, json=response)

    settings, engine, queries, location, service, queue, provider, job = configured(
        database, handler
    )
    try:
        assert Worker(queue, service).once()
        assert not Worker(queue, service).once()
        assert queries.get("jobs", job["id"])["status"] == "failed"
        assert queries.get("locations", location["id"])["initialized_at"] is None
        assert queries.rows("restaurants") == []
        assert task(engine, job)["response_json"] is not None
        assert methods == ["POST"]
        assert AIAudit(engine, settings).detail(job["id"])["job"]["has_saved_response"]
    finally:
        provider.close()


def test_migration_changes_only_unsubmitted_prompts_and_can_restore_them(database):
    settings, engine, _, queries, city, service, _ = setup(database)
    config = Config(str(settings.root_dir / "alembic.ini"))
    command.downgrade(config, "filling_v3")
    custom = "Авторский промпт до ограниченного исследования. " * 2
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE settings SET value=:prompt WHERE key='prompt'"), {"prompt": custom}
        )
    service.enqueue_public_fill(city["id"])
    other = queries.directory(query="Шуя")["rows"][0]
    service.enqueue_public_fill(other["id"])
    jobs = queries.rows("jobs")
    submitted = next(job for job in jobs if job["location_id"] == city["id"])
    pending = next(job for job in jobs if job["location_id"] == other["id"])
    recorded = {
        "input": [{"role": "user", "content": custom}],
        "text": {"format": {"name": "generation_v3"}},
    }
    with engine.begin() as connection:
        usage = str(uuid4())
        connection.execute(
            text(
                "INSERT INTO ai_usage(id,job_id,model,operation,status) VALUES(:id,:job,'fixture','responses','reserved')"
            ),
            {"id": usage, "job": submitted["id"]},
        )
        connection.execute(
            text(
                "INSERT INTO generation_requests(job_id,usage_id,state,request_json,deadline_at) VALUES(:job,:usage,'queued',CAST(:body AS jsonb),now()+interval '1 hour')"
            ),
            {"job": submitted["id"], "usage": usage, "body": json.dumps(recorded)},
        )
    command.upgrade(config, "head")
    with engine.connect() as connection:
        actual = connection.execute(
            text("SELECT value FROM settings WHERE key='prompt'")
        ).scalar_one()
    assert actual == (settings.root_dir / "data/prompts/restaurant-research-v4.txt").read_text()
    assert queries.get("jobs", pending["id"])["prompt"] == actual
    assert queries.get("jobs", submitted["id"])["prompt"] == custom
    assert task(engine, submitted)["request_json"] == recorded
    command.downgrade(config, "filling_v3")
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT value FROM settings WHERE key='prompt'")).scalar_one()
            == custom
        )
    assert queries.get("jobs", pending["id"])["prompt"] == custom
    assert queries.get("jobs", submitted["id"])["prompt"] == custom
    command.upgrade(config, "head")
    with engine.begin() as connection:
        connection.execute(text("UPDATE settings SET value='Ручная правка' WHERE key='prompt'"))
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT value FROM settings WHERE key='prompt'")).scalar_one()
            == "Ручная правка"
        )
