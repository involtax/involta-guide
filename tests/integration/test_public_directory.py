from fastapi.testclient import TestClient

from involta_guide.main import create_app

from .editor_fixture import FixtureEditor as EditorialService
from .test_editorial import CONTENT
from .test_jobs import setup


def test_home_shows_only_filled_cities_and_search_covers_all_types(database):
    settings, _, uow, queries, city, _, _ = setup(database)
    editor = EditorialService(uow)
    other = next(row for row in queries.directory(kind="resort")["rows"] if row["listed"])
    with TestClient(create_app(settings)) as client:
        assert f'href="/guide/{city["slug"]}"' not in client.get("/guide/").text
        editor.save(city["id"], CONTENT, None)
        assert f'href="/guide/{city["slug"]}"' in client.get("/guide/").text
        editor.save(other["id"], CONTENT, None)
        home = client.get("/guide/")
        assert f'href="/guide/{city["slug"]}"' in home.text
        assert f'href="/guide/{other["slug"]}"' not in home.text
        assert '<select name="kind"' not in home.text
        search = client.get("/guide/", params={"q": other["name"], "kind": "city"})
        assert f'href="/guide/{other["slug"]}"' in search.text
        assert '<select name="kind"' not in search.text


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
        assert "Записать отзыв" in page.text
