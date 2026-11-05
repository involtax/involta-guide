"""Activate destination research, preserving manual geography and recorded input."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from .editor_fixture import FixtureEditor
from .test_background_generation import task
from .test_editorial import CONTENT
from .test_jobs import setup


@pytest.mark.parametrize("manual_scope", [False, True])
def test_upgrade_removes_seeded_clues_without_changing_content_or_recorded_input(
    database, manual_scope
):
    settings, engine, uow, queries, city, service, _ = setup(database)
    FixtureEditor(uow).save(city["id"], CONTENT, city["revision"])
    config = Config(str(settings.root_dir / "alembic.ini"))
    old = ScriptDirectory.from_config(config).get_revision("city_surroundings").module
    current = ScriptDirectory.from_config(config).get_revision("unbiased_city_scope").module
    command.downgrade(config, "city_surroundings")
    original_prompt = "Авторский промпт до изменения порядка поиска. " * 2
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE settings SET value=:prompt WHERE key='prompt'"),
            {"prompt": original_prompt},
        )
        for location_id, note in old.COVERAGE_NOTES.items():
            conn.execute(
                text("UPDATE locations SET coverage_note=:note WHERE id=:id"),
                {"id": location_id, "note": note},
            )
        if manual_scope:
            conn.execute(text("UPDATE locations SET coverage_note='Ручной охват' WHERE id=34"))
    service.enqueue_public_fill(34)
    service.enqueue_public_fill(622)
    jobs = {j["location_id"]: j for j in queries.rows("jobs")}
    frozen = {"input": [{"role": "user", "content": old.COVERAGE_NOTES[622]}]}
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
    before_locations = queries.rows("locations")
    before_restaurants = queries.rows("restaurants")
    before_reviews = queries.rows("reviews")
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    for before in before_locations:
        after = queries.get("locations", before["id"])
        if before["id"] == 622 or (before["id"] == 34 and not manual_scope):
            assert after.pop("coverage_note") == ""
            before = {k: v for k, v in before.items() if k != "coverage_note"}
        assert after == before
    assert queries.rows("restaurants") == before_restaurants
    assert queries.rows("reviews") == before_reviews
    expected_jobs = {j["id"]: dict(j) for j in jobs.values()}
    expected_jobs[jobs[34]["id"]]["prompt"] = current.PROMPT
    assert {j["id"]: j for j in queries.rows("jobs")} == expected_jobs
    assert task(engine, jobs[622])["request_json"] == frozen
    assert queries.setting("prompt") == current.PROMPT
    assert (
        current.PROMPT
        == (settings.root_dir / "data/prompts/restaurant-research-v4.txt").read_text()
    )
    # A later admin edit survives downgrade; only fields actually cleared are restored.
    with engine.begin() as conn:
        conn.execute(text("UPDATE locations SET coverage_note='Новый охват' WHERE id=622"))
        if manual_scope:
            conn.execute(text("UPDATE settings SET value='Новый промпт' WHERE key='prompt'"))
            conn.execute(
                text("UPDATE jobs SET prompt='Ручное задание' WHERE id=:id"),
                {"id": jobs[34]["id"]},
            )
    command.upgrade(config, "head")
    command.downgrade(config, "city_surroundings")
    assert queries.setting("prompt") == ("Новый промпт" if manual_scope else original_prompt)
    assert queries.get("jobs", jobs[34]["id"])["prompt"] == (
        "Ручное задание" if manual_scope else original_prompt
    )
    assert queries.get("jobs", jobs[622]["id"])["prompt"] == original_prompt
    assert queries.get("locations", 622)["coverage_note"] == "Новый охват"
    assert queries.get("locations", 34)["coverage_note"] == (
        "Ручной охват" if manual_scope else old.COVERAGE_NOTES[34]
    )
    assert task(engine, jobs[622])["request_json"] == frozen


def test_new_catalogue_does_not_seed_expected_restaurants_as_search_clues():
    catalogue = json.loads((Path(__file__).parents[2] / "data/location-catalog.json").read_text())
    assert {
        r["id"]: r["coverage_note"] for r in catalogue["locations"] if r["id"] in (34, 622)
    } == {34: "", 622: ""}
