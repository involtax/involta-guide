import re
from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_guide.main import create_app


def add_location(
    engine,
    name,
    slug,
    *,
    region="Область Теста",
    aliases=None,
    listed=True,
    active=1,
    ready=False,
    parent=None,
):
    key = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text("""INSERT INTO locations(id,name,slug,region,aliases,listed,active,initialized_at,parent_id)
          VALUES(:id,:name,:slug,:region,:aliases,:listed,:active,
          CASE WHEN :ready THEN now() ELSE NULL END,:parent)"""),
            {
                "id": key,
                "name": name,
                "slug": slug,
                "region": region,
                "aliases": aliases or [],
                "listed": listed,
                "active": active,
                "ready": ready,
                "parent": parent,
            },
        )
    return key


def test_public_ranking_visibility_limit_and_full_form_share_one_search(database):
    settings, engine = database
    add_location(engine, "Тестоград", "search-exact")
    add_location(
        engine, "Город Алиас", "search-alias", aliases=["Тестоград", "Тестоград"], ready=True
    )
    for number in range(7):
        add_location(engine, f"Тестоград {number}", f"search-prefix-{number}", ready=number == 0)
    add_location(
        engine, "Алиасный сосед", "search-prefix-alias", aliases=["Тестоград Новый"], ready=True
    )
    add_location(engine, "Контекстный", "search-context", region="Тестоград", ready=True)
    add_location(engine, "Тестоград Скрытый", "search-hidden", listed=False)
    add_location(engine, "Тестоград Отключённый", "search-inactive", active=0)
    parent = add_location(engine, "Тестоград Узел", "search-hub", listed=False)
    add_location(
        engine, "Тестоград Дочерний", "search-child", listed=False, ready=True, parent=parent
    )
    with TestClient(create_app(settings)) as client:
        result = client.get("/guide/api/locations/suggest", params={"q": "Тестоград"})
        items = result.json()["items"]
        assert result.status_code == 200 and len(items) == 6
        assert [item["url"] for item in items[:3]] == [
            "/guide/search-exact",
            "/guide/search-alias",
            "/guide/search-prefix-0",
        ]
        assert all(set(item) == {"name", "context", "url"} for item in items)
        assert "search-hidden" not in result.text and "search-inactive" not in result.text
        full = client.get("/guide/", params={"q": "Тестоград"})
        urls = re.findall(r'<a class="city-link" href="([^"]+)"', full.text)
        assert urls[:6] == [item["url"] for item in items]
        assert len(urls) > 6 and len(urls) == len(set(urls))
        assert "/guide/search-context" in urls and "/guide/search-hub" in urls
        assert "/guide/search-hidden" not in urls and "/guide/search-inactive" not in urls


def test_real_catalogue_aliases_word_order_layout_and_typo_fallback(database):
    settings, engine = database
    add_location(engine, "Псевдогород", "search-real-alias", aliases=["Город Мечты"])
    with TestClient(create_app(settings)) as client:
        for query, expected in [
            ("ПлЕс", "Плёс"),
            ("новгород нижний", "Нижний Новгород"),
            ("sochi", "Сочи"),
            ("cjxb", "Сочи"),
            ("Сочии", "Сочи"),
            ("Город-Мечты", "Псевдогород"),
        ]:
            response = client.get("/guide/api/locations/suggest", params={"q": query})
            assert response.status_code == 200, response.text
            assert response.json()["items"][0]["name"] == expected, (query, response.text)
        assert (
            client.get("/guide/api/locations/suggest", params={"q": "жцфыщбъь"}).json()["items"]
            == []
        )
        assert client.get("/guide/api/locations/suggest", params={"q": "жц"}).json()["items"] == []


def test_suggestions_do_not_track_visitors_start_work_or_query_for_short_input(
    database, monkeypatch
):
    settings, engine = database
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/guide/api/locations/suggest?q=плес")
        assert response.json()["items"][0]["name"] == "Плёс"
        assert response.headers["cache-control"] == "public, max-age=30"
        assert "set-cookie" not in response.headers
        assert (
            client.get("/guide/api/locations/suggest", params={"q": "x" * 101}).status_code == 422
        )

        def forbidden(*args, **kwargs):
            raise AssertionError("No SQL for an empty/short normalized query")

        monkeypatch.setattr(app.state.context.queries, "public_directory", forbidden)
        for query in ("", "я", "%_", "\\\x00"):
            assert client.get("/guide/api/locations/suggest", params={"q": query}).json() == {
                "items": []
            }
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM audience_daily")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM jobs")).scalar_one() == 0


def test_index_migration_rolls_back_without_changing_catalogue_or_shared_extension(database):
    settings, engine = database
    config = Config(str(settings.root_dir / "alembic.ini"))
    with engine.connect() as connection:
        before = connection.execute(text("SELECT count(*) FROM locations")).scalar_one()
        assert connection.execute(text("SELECT to_regclass('locations_name_trgm')")).scalar_one()
    command.downgrade(config, "audience_stats")
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT to_regclass('locations_name_trgm')")).scalar_one()
            is None
        )
        assert connection.execute(text("SELECT count(*) FROM locations")).scalar_one() == before
        assert (
            connection.execute(text("SELECT public.similarity('сочи','сочии')")).scalar_one() > 0.45
        )
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT to_regclass('locations_name_trgm')")).scalar_one()
