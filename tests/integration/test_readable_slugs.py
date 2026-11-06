"""Upgrade populated geography, keeping content and dropping obsolete URLs."""

import json
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow
from involta_guide.infrastructure.seed import seed
from involta_guide.main import create_app

from .test_editorial import CONTENT, services
from .test_web import login

pytestmark = pytest.mark.full_catalog


def previous_catalogue(database):
    settings, engine = database
    plan = json.loads((settings.root_dir / "migrations/data/readable_slugs.json").read_text())
    with engine.begin() as connection:
        for row in plan["locations"]:
            connection.execute(text("UPDATE locations SET slug=:old WHERE slug=:new"), row)
        for row in plan["redirects"]:
            connection.execute(
                text("UPDATE location_slug_redirects SET slug=:old WHERE slug=:new"), row
            )
        connection.execute(text("UPDATE alembic_version SET version_num='audience_stats'"))
        connection.execute(text("DELETE FROM settings WHERE key='python_catalog_v1'"))
    return plan


def test_upgrade_preserves_content_links_and_logs_without_old_redirects(database):
    settings, engine = database
    editor, queries, factory = services(database)
    with factory() as work:
        city = work.repo.by_slug("ples")[0]
    editor.save(city["id"], CONTENT, None)
    before = queries.location(city["id"])
    reviews = queries.rows("reviews")
    previous_catalogue(database)
    with factory() as work:
        work.repo.add("jobs", {"id": str(uuid4()), "kind": "generate", "location_id": city["id"]})
        locations_before = work.repo.rows("locations", limit=100000)
        relations_before = {r["id"]: r["nearby_location_ids"] for r in locations_before}
        assert work.repo.rows("request_logs")[0]["path"] == "/logs/plyos/1"

    command.upgrade(Config(str(settings.root_dir / "alembic.ini")), "readable_slugs")
    assert seed(PostgresUow(engine), settings.root_dir) == 0
    assert seed(PostgresUow(engine), settings.root_dir) == 0
    after = queries.location(city["id"])
    assert after["content"] == before["content"]
    assert queries.rows("reviews") == reviews
    with factory() as work:
        locations_after = work.repo.rows("locations", limit=100000)
        assert {row["id"]: row["parent_id"] for row in locations_after} == {
            row["id"]: row["parent_id"] for row in locations_before
        }
        assert {r["id"]: r["nearby_location_ids"] for r in locations_after} == relations_before
        assert work.repo.rows("request_logs")[0]["path"] == "/logs/ples/1"
        assert not any(row["slug"] == "plyos" for row in work.repo.rows("location_slug_redirects"))
        assert len(work.repo.rows("audit", {"action": "location_slug"}, limit=1000)) == 283
    with TestClient(create_app(settings)) as client:
        assert client.get("/guide/ples").status_code == 200
        assert client.get("/guide/plyos", follow_redirects=False).status_code == 404
        assert client.get("/guide/logs/ples/1").status_code == 200
        assert client.get("/guide/logs/plyos/1", follow_redirects=False).status_code == 404
        sitemap = client.get("/guide/sitemap.xml").text
        assert "/guide/ples<" in sitemap and "/guide/plyos<" not in sitemap


@pytest.mark.parametrize("alias", [False, True])
def test_occupied_target_aborts_entire_upgrade(database, alias):
    settings, engine = database
    previous_catalogue(database)
    with engine.begin() as connection:
        if alias:
            connection.execute(
                text(
                    "INSERT INTO location_slug_redirects(slug,location_id) "
                    "SELECT 'ples',id FROM locations WHERE slug='ivanovo'"
                )
            )
        else:
            connection.execute(text("UPDATE locations SET slug='ples' WHERE slug='ivanovo'"))
    with pytest.raises(RuntimeError, match="URL ples уже занят"):
        command.upgrade(Config(str(settings.root_dir / "alembic.ini")), "readable_slugs")
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
            == "audience_stats"
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM audit WHERE action='location_slug'")
            ).scalar()
            == 0
        )
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM locations WHERE slug IN ('plyos','sankt-peterburg','khimki')"
                )
            ).scalar()
            == 3
        )


def test_manual_url_survives_upgrade_and_catalogue_import(database):
    settings, engine = database
    previous_catalogue(database)
    with engine.begin() as connection:
        connection.execute(text("UPDATE locations SET slug='my-ples' WHERE slug='plyos'"))
    command.upgrade(Config(str(settings.root_dir / "alembic.ini")), "readable_slugs")
    assert seed(PostgresUow(engine), settings.root_dir) == 0
    with PostgresUow(engine) as work:
        assert work.repo.by_slug("my-ples")[0]["name"] == "Плёс"
        assert len(work.repo.rows("locations", limit=100000)) == 1448


def test_new_location_suggests_url_but_preserves_manual_choice(database):
    settings, _ = database
    with TestClient(create_app(settings)) as client:
        csrf = login(client)
        data = {
            "_csrf": csrf,
            "name": "Новый Хутор",
            "region": "Тестовая область",
            "slug": "",
            "in_phrase": "в Новом Хуторе",
            "kind": "city",
            "priority": "medium",
            "active": "on",
            "listed": "on",
        }
        headers = {"origin": "http://testserver"}
        response = client.post("/guide/admin/locations", data=data, headers=headers)
        assert response.url.path == "/guide/admin/locations/novy-hutor"
        duplicate = client.post("/guide/admin/locations", data=data, headers=headers)
        assert "URL уже занят" in duplicate.text
        data.update(name="Совсем другое название", slug="novy-hutor")
        response = client.post("/guide/admin/locations/novy-hutor", data=data, headers=headers)
        assert response.url.path == "/guide/admin/locations/novy-hutor"
        data.update(slug="my-chosen-url")
        response = client.post("/guide/admin/locations", data=data, headers=headers)
        assert response.url.path == "/guide/admin/locations/my-chosen-url"
