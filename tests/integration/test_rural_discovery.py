"""Production omission, local researched response, and unchanged publication rules."""

import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_guide.infrastructure.jobs.worker import Worker
from involta_guide.main import create_app

from .editor_fixture import FixtureEditor
from .test_background_generation import configured, envelope, task
from .test_editorial import CONTENT
from .test_jobs import setup

FIXTURES = Path(__file__).parents[1] / "fixtures"
ORIGINAL_TOP = ["Moroshka", "Зангезур", "Едем&Едим"]


@pytest.mark.parametrize("scenario", ["production", "local_research", "low_score", "missing_phone"])
def test_real_omission_and_locally_researched_response_use_the_same_city_ranking(
    database, scenario
):
    # The added record is authored after web research, not an independent model/API result.
    raw = json.loads((FIXTURES / "medvezhegorsk-response-2.json").read_text())
    original = deepcopy(raw)
    if scenario != "production":
        runa = json.loads((FIXTURES / "runa-local-candidate.json").read_text())
        if scenario == "low_score":
            runa["scores"] = dict.fromkeys(runa["scores"], 2)
        elif scenario == "missing_phone":
            runa.update(
                status="needs_verification",
                phone="",
                verificationIssues=["phone"],
                reason="Не подтверждён телефон — искусственный отрицательный контроль",
            )
        raw["candidates"].append(runa)
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        result = envelope()
        result["output"][0]["content"][0]["text"] = json.dumps(raw, ensure_ascii=False)
        return httpx.Response(200, json=result)

    settings, engine, queries, city, service, queue, provider, job = configured(
        database, handler, location_query="Медвежьегорск"
    )
    try:
        count = len(queries.rows("locations"))
        assert city["id"] == 622
        assert Worker(queue, service).once()
        stored = queries.get("jobs", job["id"])
        assert stored["status"] == "done" and stored["applied"]
        cards = queries.location(city["id"])["content"]["restaurants"]
        expected = ["Руна", *ORIGINAL_TOP[:2]] if scenario == "local_research" else ORIGINAL_TOP
        assert [r["name"] for r in cards] == expected
        assert len(queries.rows("locations")) == count
        if scenario == "local_research":
            assert "Кажма" in cards[0]["description"]
            assert cards[0]["mapsUrl"] == runa["mapsUrl"]
            entry = next(r for r in queries.rows("restaurants") if r["name"] == "Руна")
            assert entry["location_id"] == 622
        request_prompt = requests[0]["input"][1]["content"]
        for clue in ("Руна", "Заонежье", "Кажма", "zaonezhie.com", "Винотеррия"):
            assert clue not in request_prompt
        assert raw["candidates"][: len(original["candidates"])] == original["candidates"]
        saved = task(engine, job)["response_json"]
        assert json.loads(saved["output"][0]["content"][0]["text"]) == raw
        with TestClient(create_app(settings)) as client:
            page = client.get("/guide/medvezhegorsk")
            assert page.status_code == 200
            assert ("Руна" in page.text) == (scenario == "local_research")
        proof = Path("var/evaluations/rural-discovery")
        proof.mkdir(parents=True, exist_ok=True)
        (proof / (scenario + ".json")).write_text(
            json.dumps(
                {
                    "scenario": scenario,
                    "public": expected,
                    "ranking": [
                        {k: r[k] for k in ("name", "totalScore")}
                        for r in stored["report"]["rankingReport"]
                    ],
                    "simulated_provider_response": scenario != "production",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        provider.close()


@pytest.mark.parametrize("manual_edits", [False, True])
def test_prompt_activation_preserves_recorded_requests_and_can_restore_previous_values(
    database, manual_edits
):
    settings, engine, uow, queries, city, service, _ = setup(database)
    FixtureEditor(uow).save(city["id"], CONTENT, city["revision"])
    config = Config(str(settings.root_dir / "alembic.ini"))
    current = ScriptDirectory.from_config(config).get_revision("rural_discovery").module
    command.downgrade(config, "hybrid_search")
    previous = "Прежний авторский промпт для новых исследований. " * 2
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE settings SET value=:prompt WHERE key='prompt'"), {"prompt": previous}
        )
    for key in (34, 622):
        service.enqueue_public_fill(key)
    jobs = {j["location_id"]: j for j in queries.rows("jobs")}
    frozen = {"input": [{"role": "user", "content": previous}], "schema": "recorded"}
    with engine.begin() as conn:
        usage = str(uuid4())
        conn.execute(
            text("""INSERT INTO ai_usage(id,job_id,model,operation,status)
            VALUES(:id,:job,'fixture','responses','reserved')"""),
            {"id": usage, "job": jobs[622]["id"]},
        )
        conn.execute(
            text("""INSERT INTO generation_requests
            (job_id,usage_id,state,request_json,deadline_at)
            VALUES(:job,:usage,'queued',CAST(:body AS jsonb),now()+interval '1 hour')"""),
            {"job": jobs[622]["id"], "usage": usage, "body": json.dumps(frozen)},
        )
    locations = queries.rows("locations")
    restaurants = queries.rows("restaurants")
    reviews = queries.rows("reviews")
    command.upgrade(config, "rural_discovery")
    assert queries.setting("prompt") == current.PROMPT
    assert queries.get("jobs", jobs[34]["id"])["prompt"] == current.PROMPT
    assert queries.get("jobs", jobs[622]["id"])["prompt"] == previous
    assert task(engine, jobs[622])["request_json"] == frozen
    assert queries.rows("locations") == locations
    assert queries.rows("restaurants") == restaurants
    assert queries.rows("reviews") == reviews
    if manual_edits:
        with engine.begin() as conn:
            conn.execute(text("UPDATE settings SET value='Ручной промпт' WHERE key='prompt'"))
            conn.execute(
                text("UPDATE jobs SET prompt='Ручное задание' WHERE id=:id"), {"id": jobs[34]["id"]}
            )
    command.upgrade(config, "rural_discovery")
    command.downgrade(config, "hybrid_search")
    assert queries.setting("prompt") == ("Ручной промпт" if manual_edits else previous)
    assert queries.get("jobs", jobs[34]["id"])["prompt"] == (
        "Ручное задание" if manual_edits else previous
    )
    assert queries.get("jobs", jobs[622]["id"])["prompt"] == previous
    assert task(engine, jobs[622])["request_json"] == frozen


def test_current_repository_prompt_is_active_after_upgrade(database):
    settings, _, _, queries, _, _, _ = setup(database)
    assert (
        queries.setting("prompt")
        == (settings.root_dir / "data/prompts/restaurant-research-v4.txt").read_text()
    )
