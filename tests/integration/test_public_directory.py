import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from involta_guide.main import create_app

from .editor_fixture import FixtureEditor as EditorialService
from .test_editorial import CONTENT
from .test_jobs import setup


def test_home_panorama_links_and_choices_cover_all_types(database):
    settings, _, uow, queries, city, _, _ = setup(database)
    editor = EditorialService(uow)
    other = next(row for row in queries.directory(kind="resort")["rows"] if row["listed"])
    with TestClient(create_app(settings)) as client:
        assert f'href="/guide/{city["slug"]}"' in client.get("/guide/").text
        editor.save(city["id"], CONTENT, None)
        assert f'href="/guide/{city["slug"]}"' in client.get("/guide/").text
        editor.save(other["id"], CONTENT, None)
        home = client.get("/guide/")
        assert f'href="/guide/{city["slug"]}"' in home.text
        assert "Узнаёшь город?" in home.text and 'class="city-grid"' not in home.text
        assert f'href="/guide/{other["slug"]}"' not in home.text
        assert '<select name="kind"' not in home.text
        catalogue = client.get("/guide/api/locations/catalogue").json()
        assert any(item["location"] == other["slug"] for item in catalogue)


def test_city_page_omits_removed_metadata_and_review_controls(database):
    settings, _, uow, _, city, _, _ = setup(database)
    editor = EditorialService(uow)
    editor.save(city["id"], CONTENT, None)
    with TestClient(create_app(settings)) as client:
        page = client.get("/guide/" + city["slug"])
        for removed in (
            'class="breadcrumbs"',
            "ИНВОЛЬТА ГИД /",
            "Редакционная подборка · обновлено",
            "01 мест",
            "data-consent",
            "Согласен на хранение",
            "Изменения проверит редактор.",
        ):
            assert removed not in page.text
        assert "Тестовый ресторан" in page.text
        assert page.text.count('data-target="guide-voice"') == 1
        assert "Записать отзыв" in client.get("/guide/voice/widget.v1.js").text


def test_guide_serves_the_complete_pinned_vad_bundle_without_voice_service(database):
    settings, _ = database
    root = Path(__file__).parents[2] / "src/involta_voice/static/vad"
    manifest = json.loads((root / "manifest.json").read_text())
    with TestClient(create_app(settings)) as client:
        for name, expected in manifest["files"].items():
            response = client.get("/guide/voice/vad/" + name)
            assert response.status_code == 200, name
            assert hashlib.sha256(response.content).hexdigest() == expected["sha256"], name
            if name.endswith(".wasm"):
                assert response.headers["content-type"] == "application/wasm"
            elif name.endswith((".js", ".mjs")):
                assert "javascript" in response.headers["content-type"]
        assert client.get("/guide/voice/vad/%2e%2e%2fwidget.v1.js").status_code == 404


def test_home_omits_unavailable_panorama_cities(database, monkeypatch):
    from sqlalchemy import text

    from involta_guide.web.routes import public

    settings, engine = database
    with engine.begin() as connection:
        connection.execute(text("UPDATE locations SET active=0 WHERE slug='ivanovo'"))
        connection.execute(text("UPDATE locations SET listed=false WHERE slug='ples'"))
    original = public.panorama_for
    monkeypatch.setattr(
        public, "panorama_for", lambda slug: original(slug) if slug in {"ivanovo", "ples"} else None
    )
    with TestClient(create_app(settings)) as client:
        home = client.get("/guide/")
        assert home.status_code == 200
        assert 'class="home-cities"' not in home.text
        assert "home-cities.js" not in home.text
        assert 'class="home-city"' not in home.text


@pytest.mark.full_catalog
def test_home_random_panorama_selection_has_no_duplicates_and_handles_small_pool(
    database, monkeypatch
):
    from random import Random

    from involta_guide.web.routes import public

    settings, _ = database
    monkeypatch.setattr(public, "sample", Random(42).sample)
    with TestClient(create_app(settings)) as client:
        selections = []
        for _ in range(3):
            response = client.get("/guide/")
            cities = response.context["panorama_cities"]
            slugs = [city["slug"] for city in cities]
            assert len(slugs) == len(set(slugs)) == 7
            assert all(
                city["kind"] == "city" and public.panorama_for(city["slug"]) for city in cities
            )
            selections.append(slugs)
        assert selections[0] != selections[1]
        assert len({slug for selection in selections for slug in selection}) > 7
        original = public.panorama_for
        monkeypatch.setattr(
            public, "panorama_for", lambda slug: original(slug) if slug == "ples" else None
        )
        cities = client.get("/guide/").context["panorama_cities"]
        assert [city["slug"] for city in cities] == ["ples"]
