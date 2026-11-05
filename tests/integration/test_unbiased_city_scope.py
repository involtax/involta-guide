"""Remove only seeded clues, preserving manual geography and already sent input."""

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
    command.downgrade(config, "city_surroundings")
    with engine.begin() as conn:
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
    assert {j["id"]: j for j in queries.rows("jobs")} == {j["id"]: j for j in jobs.values()}
    assert task(engine, jobs[622])["request_json"] == frozen
    assert queries.setting("prompt") == old.PROMPT
    # A later admin edit survives downgrade; only fields actually cleared are restored.
    with engine.begin() as conn:
        conn.execute(text("UPDATE locations SET coverage_note='Новый охват' WHERE id=622"))
    command.downgrade(config, "city_surroundings")
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
