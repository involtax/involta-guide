import json
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


@pytest.mark.parametrize("has_intro_rule", [False, True])
@pytest.mark.parametrize("later_edit", [False, True])
def test_intro_migration_preserves_manual_prompt_content_jobs_and_later_edits(
    database, has_intro_rule, later_edit
):
    settings, engine, uow, queries, city, service, _ = setup(database)
    config = Config(str(settings.root_dir / "alembic.ini"))
    migration = ScriptDirectory.from_config(config).get_revision("short_city_intro").module
    command.downgrade(config, "vip_visitors")
    before = "Авторский порядок поиска и источники.\n"
    if has_intro_rule:
        before += "intro — два предложения с названиями ресторанов.\n"
    before += "Ручные требования к оценкам остаются.\n"
    with engine.begin() as conn:
        conn.execute(text("UPDATE settings SET value=:value WHERE key='prompt'"), {"value": before})
    shuya = queries.directory(query="Шуя")["rows"][0]
    kostroma = queries.directory(query="Кострома")["rows"][0]
    FixtureEditor(uow).save(kostroma["id"], CONTENT, None)
    service.enqueue_public_fill(city["id"])
    service.enqueue_public_fill(shuya["id"])
    jobs = queries.rows("jobs")
    frozen = {"input": [{"role": "user", "content": before}]}
    with engine.begin() as conn:
        usage = str(uuid4())
        conn.execute(
            text(
                "INSERT INTO ai_usage(id,job_id,model,operation,status) VALUES(:id,:job,'fixture','responses','reserved')"
            ),
            {"id": usage, "job": jobs[0]["id"]},
        )
        conn.execute(
            text("""INSERT INTO generation_requests(job_id,usage_id,state,request_json,deadline_at)
            VALUES(:job,:usage,'queued',CAST(:body AS jsonb),now()+interval '1 hour')"""),
            {"job": jobs[0]["id"], "usage": usage, "body": json.dumps(frozen)},
        )
    locations, restaurants, reviews = (
        queries.rows(table) for table in ("locations", "restaurants", "reviews")
    )
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    expected = (
        before.replace("intro — два предложения с названиями ресторанов.", migration.INTRO)
        if has_intro_rule
        else before.rstrip() + "\n\n" + migration.INTRO + "\n"
    )
    assert queries.setting("prompt") == expected
    assert queries.rows("jobs") == jobs
    assert task(engine, jobs[0])["request_json"] == frozen
    assert queries.rows("locations") == locations
    assert queries.rows("restaurants") == restaurants
    assert queries.rows("reviews") == reviews
    # New jobs use the updated editable prompt without changing an existing job.
    new_city = queries.directory(query="Плёс")["rows"][0]
    service.enqueue_public_fill(new_city["id"])
    assert (
        next(job for job in queries.rows("jobs") if job["location_id"] == new_city["id"])["prompt"]
        == expected
    )
    if later_edit:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE settings SET value='Новая ручная редакция' WHERE key='prompt'")
            )
    command.downgrade(config, "vip_visitors")
    assert queries.setting("prompt") == ("Новая ручная редакция" if later_edit else before)


def test_new_installation_template_and_migration_use_the_same_intro_rule():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    migration = ScriptDirectory.from_config(config).get_revision("short_city_intro").module
    prompt = (root / "data/prompts/restaurant-research-v4.txt").read_text()
    assert (
        next(line for line in prompt.splitlines() if line.startswith("intro —")) == migration.INTRO
    )
