"""Destination scope without extra locations or live research calls."""

import json
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_guide.infrastructure.jobs.worker import Worker
from involta_guide.main import create_app

from .editor_fixture import FixtureEditor
from .test_background_generation import configured, envelope, task
from .test_editorial import CONTENT
from .test_filling_top3 import shortlist
from .test_jobs import setup

SPEC = spec_from_file_location(
    "city_surroundings",
    Path(__file__).resolve().parents[2] / "migrations/versions/city_surroundings.py",
)
assert SPEC and SPEC.loader
CONTRACT = module_from_spec(SPEC)
SPEC.loader.exec_module(CONTRACT)


@pytest.mark.parametrize(
    ("city", "location_id", "name", "description", "source"),
    [
        (
            "Анапа",
            34,
            "Винотеррия",
            "Ресторан у хутора Семигорский, на территории Новороссийска. Южная кухня.",
            "https://vinoterria.com/",
        ),
        (
            "Медвежьегорск",
            622,
            "Руна",
            "Ресторан отеля «Заонежье», деревня Кажма, 74. Карельская кухня.",
            "https://zaonezhie.com/restaurant",
        ),
    ],
)
def test_countryside_competes_in_the_city_list_with_its_real_locality(
    database, city, location_id, name, description, source
):
    # A deterministic provider fixture tests storage/ranking, not actual model research quality.
    raw = shortlist(5)
    raw["candidates"][0]["scores"] = dict.fromkeys(raw["candidates"][0]["scores"], 8)
    countryside = raw["candidates"][1]
    countryside.update(name=name, description=description, sources=[source])
    countryside["scores"] = dict.fromkeys(countryside["scores"], 9)
    raw["candidates"][2]["scores"] = dict.fromkeys(countryside["scores"], 7)
    raw["candidates"][3].update(
        name="Неясный филиал",
        status="needs_verification",
        verificationIssues=["location"],
        reason="Не разрешено противоречие адресов",
    )
    raw["candidates"][4].update(
        name="Далёкое направление",
        status="excluded",
        exclusionCode="outside_area",
        reason="Другое самостоятельное направление за пределами охвата",
        scores={},
        evidence=[],
    )
    raw["discoveredRestaurants"] = [
        {"name": "Неясный филиал", "sources": []},
        {"name": "Далёкое направление", "sources": []},
    ]
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "resp_surroundings",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": json.dumps(raw, ensure_ascii=False)}
                        ],
                    }
                ],
            },
        )

    settings, engine, queries, location, service, queue, provider, job = configured(
        database, handler, location_query=city
    )
    try:
        initial_location_count = len(queries.rows("locations"))
        assert location["id"] == location_id
        assert Worker(queue, service).once()
        assert queries.get("jobs", job["id"])["status"] == "done"
        prompt = requests[0]["input"][1]["content"]
        assert CONTRACT.COVERAGE_NOTES[location_id] in prompt
        assert name in prompt
        assert "Только " + city not in prompt
        winners = queries.location(location_id)["content"]["restaurants"]
        assert [r["name"] for r in winners] == [name, "Кандидат 00", "Кандидат 02"]
        assert winners[0]["description"] == description
        assert winners[0]["mapsUrl"] == countryside["mapsUrl"]
        assert {r["location_id"] for r in queries.rows("restaurants")} == {location_id}
        assert {r["name"] for r in queries.rows("restaurants")} == {
            name,
            "Кандидат 00",
            "Кандидат 02",
        }
        assert len(queries.rows("locations")) == initial_location_count
        assert (
            json.loads(task(engine, job)["response_json"]["output"][0]["content"][0]["text"]) == raw
        )
        with TestClient(create_app(settings)) as client:
            html = client.get("/guide/" + location["slug"]).text
            assert description in html
            assert countryside["mapsUrl"] in html
    finally:
        provider.close()


@pytest.mark.parametrize(
    "custom_scope", ["", "Иваново и Шуйский район: лучшие загородные рестораны"]
)
def test_default_scope_includes_surroundings_and_explicit_scope_is_preserved(
    database, custom_scope
):
    prompts = []

    def handler(request):
        prompts.append(json.loads(request.content)["input"][1]["content"])
        return httpx.Response(200, json=envelope())

    _, engine, _, location, service, queue, provider, _ = configured(database, handler)
    try:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE locations SET coverage_note=:note WHERE id=:id"),
                {"note": custom_scope, "id": location["id"]},
            )
        assert Worker(queue, service).once()
        context = json.loads(prompts[0].split("ОБЯЗАТЕЛЬНЫЙ ГЕОГРАФИЧЕСКИЙ КОНТЕКСТ\n")[1])
        if custom_scope:
            assert context["scope"] == custom_scope
        else:
            assert context["scope"].startswith("Иваново, ")
            assert "гастрономические окрестности" in context["scope"]
            assert "административная граница не ограничивает" in context["scope"]
    finally:
        provider.close()


@pytest.mark.parametrize("manual_edits", [False, True])
def test_scope_migration_preserves_content_and_frozen_requests_and_can_undo(database, manual_edits):
    settings, engine, uow, queries, city, service, _ = setup(database)
    FixtureEditor(uow).save(city["id"], CONTENT, city["revision"])
    config = Config(str(settings.root_dir / "alembic.ini"))
    command.downgrade(config, "location_ids")
    original_prompt = "Прежний авторский промпт. " * 4
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE settings SET value=:value WHERE key='prompt'"), {"value": original_prompt}
        )
        conn.execute(
            text("UPDATE locations SET coverage_note='Прежний охват' WHERE id IN (34,622)")
        )
    for location_id in (34, 622):
        service.enqueue_public_fill(location_id)
    jobs = {j["location_id"]: j for j in queries.rows("jobs")}
    submitted, pending = jobs[622], jobs[34]
    frozen = {"input": [{"role": "user", "content": original_prompt}], "marker": "frozen"}
    with engine.begin() as conn:
        usage = str(uuid4())
        conn.execute(
            text("""INSERT INTO ai_usage(id,job_id,model,operation,status)
            VALUES(:id,:job,'fixture','responses','reserved')"""),
            {"id": usage, "job": submitted["id"]},
        )
        conn.execute(
            text("""INSERT INTO generation_requests
            (job_id,usage_id,state,request_json,deadline_at)
            VALUES(:job,:usage,'queued',CAST(:body AS jsonb),now()+interval '1 hour')"""),
            {"job": submitted["id"], "usage": usage, "body": json.dumps(frozen)},
        )
        # An already filled page is not regenerated by a prompt contract migration.
        conn.execute(
            text("""UPDATE locations SET intro='Сохранённое вступление',
            initialized_at=now() WHERE id=34""")
        )
    before_locations = queries.rows("locations")
    before_catalogue = queries.rows("restaurants")
    before_reviews = queries.rows("reviews")
    assert before_catalogue and before_reviews
    command.upgrade(config, "head")
    assert queries.setting("prompt") == CONTRACT.PROMPT
    assert (
        CONTRACT.PROMPT
        == (settings.root_dir / "data/prompts/restaurant-research-v4.txt").read_text()
    )
    for before in before_locations:
        after = queries.get("locations", before["id"])
        if before["id"] in CONTRACT.COVERAGE_NOTES:
            assert after.pop("coverage_note") == CONTRACT.COVERAGE_NOTES[before["id"]]
            before = {k: v for k, v in before.items() if k != "coverage_note"}
        assert after == before
    assert queries.rows("restaurants") == before_catalogue
    assert queries.rows("reviews") == before_reviews
    assert queries.get("jobs", pending["id"])["prompt"] == CONTRACT.PROMPT
    assert queries.get("jobs", submitted["id"])["prompt"] == original_prompt
    assert task(engine, submitted)["request_json"] == frozen
    if manual_edits:
        with engine.begin() as conn:
            conn.execute(text("UPDATE settings SET value='Ручная правка' WHERE key='prompt'"))
            conn.execute(text("UPDATE locations SET coverage_note='Ручной охват' WHERE id=34"))
            conn.execute(
                text("UPDATE jobs SET prompt='Ручное задание' WHERE id=:id"), {"id": pending["id"]}
            )
    # An already applied migration never overwrites subsequent admin edits.
    command.upgrade(config, "head")
    command.downgrade(config, "location_ids")
    assert queries.setting("prompt") == ("Ручная правка" if manual_edits else original_prompt)
    assert queries.get("locations", 34)["coverage_note"] == (
        "Ручной охват" if manual_edits else "Прежний охват"
    )
    assert queries.get("locations", 622)["coverage_note"] == "Прежний охват"
    assert queries.get("jobs", pending["id"])["prompt"] == (
        "Ручное задание" if manual_edits else original_prompt
    )
    assert queries.get("jobs", submitted["id"])["prompt"] == original_prompt
    assert task(engine, submitted)["request_json"] == frozen
