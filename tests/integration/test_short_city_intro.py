import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations

from involta_guide.catalogue.service import CityContent

from .test_incremental_content import ContentAI
from .test_jobs import setup

ROOT = Path(__file__).resolve().parents[2]


def migration():
    spec = importlib.util.spec_from_file_location(
        "short_city_intro", ROOT / "migrations/versions/short_city_intro.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("saved", ["default", "default_crlf", "custom"])
def test_transition_updates_only_old_default_and_preserves_queued_requests(database, saved):
    _, engine, uow, queries, city, *_ = setup(database, ContentAI())
    change = migration()
    value = {
        "default": change.OLD + "\n",
        "default_crlf": (change.OLD + "\n").replace("\n", "\r\n"),
        "custom": "Мой собственный художественный промпт для вступления.",
    }[saved]
    with uow() as work:
        work.repo.set_setting("content_intro_prompt", value)
    content = CityContent(uow)
    identity = content.enqueue(city["id"], "intro")
    before = queries.get("jobs", identity)
    with engine.begin() as conn:
        change.op = Operations(MigrationContext.configure(conn))
        change.upgrade()
    expected = value if saved == "custom" else change.NEW
    assert queries.setting("content_intro_prompt") == expected
    assert queries.get("jobs", identity) == before
    assert content.prepare(city["id"], "intro")["prompt"] == expected
    with engine.begin() as conn:
        change.op = Operations(MigrationContext.configure(conn))
        change.downgrade()
    assert queries.setting("content_intro_prompt") == (
        value if saved == "custom" else change.OLD + "\n"
    )
    assert queries.get("jobs", identity) == before


def test_intro_has_short_default_and_no_old_text_or_restaurant_instructions(database):
    _, _, uow, queries, city, *_ = setup(database, ContentAI())
    previous = "Уникальный прежний текст, который модель не должна получать."
    with uow() as work:
        work.repo.change("locations", city["id"], {"intro": previous})
    content = CityContent(uow)
    prepared = content.prepare(city["id"], "intro")
    assert prepared["prompt"] == (ROOT / "data/prompts/city-introduction-v2.txt").read_text()
    assert previous not in str(prepared["input"]) and "intro" not in prepared["input"]
    assert "Текущий текст дан" not in prepared["prompt"]
    assert "mapsUrl" not in prepared["system"] and "openingHours" not in prepared["system"]
    assert "55" in prepared["system"]
    assert "mapsUrl" in content.prepare(city["id"], "discover")["system"]
