import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text

from involta_guide.main import create_app

pytestmark = pytest.mark.full_catalog


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
    with engine.begin() as connection:
        key = connection.execute(
            text("""INSERT INTO locations(name,slug,region,aliases,listed,active,initialized_at,parent_id)
          VALUES(:name,:slug,:region,:aliases,:listed,:active,
          CASE WHEN :ready THEN now() ELSE NULL END,:parent) RETURNING id"""),
            {
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
    return key.scalar_one()


def test_catalogue_request_has_compact_fields_validators_and_no_visitors_or_jobs(database):
    settings, engine = database
    key = add_location(engine, "Открытый Тест", "snapshot-open")
    add_location(engine, "Закрытый Тест", "snapshot-hidden", listed=False)
    add_location(engine, "Выключенный Тест", "snapshot-inactive", active=0)
    parent = add_location(engine, "Полезный Родитель", "snapshot-parent", listed=False)
    add_location(
        engine, "Наполненный Дочерний", "snapshot-child", listed=False, ready=True, parent=parent
    )
    selects = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "SELECT l.name,l.slug,l.region,l.kind,p.name parent_name" in statement:
            selects.append(statement)

    app = create_app(settings)
    search_engine = app.state.context.engine
    event.listen(search_engine, "before_cursor_execute", capture)
    try:
        with TestClient(app) as client:
            endpoint = "/guide/api/locations/catalogue"
            response = client.get(endpoint)
            items = response.json()
            locations = {item["location"] for item in items}
            assert len(selects) == 1
            assert all(set(item) == {"name", "context", "location"} for item in items)
            assert {"snapshot-open", "snapshot-parent", "snapshot-child"} <= locations
            assert "snapshot-hidden" not in locations and "snapshot-inactive" not in locations
            assert all("/guide/" not in location for location in locations)
            assert (
                next(item for item in items if item["location"] == "snapshot-open")["context"]
                == "Область Теста"
            )
            assert "set-cookie" not in response.headers
            assert response.headers["cache-control"] == "public, max-age=0, must-revalidate"
            etag = response.headers["etag"]
            cached = client.get(endpoint, headers={"If-None-Match": '"old", W/' + etag})
            assert cached.status_code == 304 and cached.content == b""
            assert cached.headers["etag"] == etag
            with engine.begin() as connection:
                assert (
                    connection.execute(text("SELECT count(*) FROM audience_daily")).scalar_one()
                    == 0
                )
                connection.execute(
                    text("UPDATE locations SET name=:name WHERE id=:id"),
                    {"name": "Новое Название", "id": key},
                )
            updated = client.get(endpoint, headers={"If-None-Match": etag})
            assert updated.status_code == 200 and updated.headers["etag"] != etag
            assert (
                next(item for item in updated.json() if item["location"] == "snapshot-open")["name"]
                == "Новое Название"
            )
            assert len(selects) == 3
            home = client.get("/guide/")
            assert 'id="location-catalogue"' not in home.text
            assert len(selects) == 3
            assert client.get("/guide/api/locations/suggest?q=и").status_code == 404
            assert (
                client.get("/guide/", params={"q": "x" * 101}, follow_redirects=False).status_code
                == 308
            )
    finally:
        event.remove(search_engine, "before_cursor_execute", capture)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM jobs")).scalar_one() == 0


def test_public_home_has_no_search_form_results_or_parameter_pages(database):
    settings, _ = database
    with TestClient(create_app(settings)) as client:
        home = client.get("/guide/")
        assert 'name="q"' not in home.text
        assert "search-choose" in home.text and "Выбери" in home.text
        assert "Результаты поиска" not in home.text and 'class="cities"' not in home.text
        assert "page_query" not in home.text and "?q=" not in home.text
        for parameters in (
            {"q": "Иваново"},
            {"page": "2"},
            {"q": "x" * 101},
            {"anything": "value"},
        ):
            response = client.get("/guide/", params=parameters, follow_redirects=False)
            assert response.status_code == 308 and response.headers["location"] == "/guide/"
            slash = client.get("/guide", params=parameters, follow_redirects=False)
            assert slash.status_code == 302
            assert slash.headers["location"] == "/guide/?" + slash.request.url.query.decode()
            final = client.get(slash.headers["location"], follow_redirects=False)
            assert final.status_code == 308 and final.headers["location"] == "/guide/"


def test_catalogue_returns_untrusted_names_as_json_and_preserves_non_city_type(database):
    settings, engine = database
    name = '</script><img src=x onerror="window.__searchXss=true">'
    add_location(engine, name, "snapshot-unsafe")
    district = add_location(engine, "Тестовый район", "snapshot-district")
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE locations SET kind='district' WHERE id=:id"), {"id": district}
        )
    with TestClient(create_app(settings)) as client:
        response = client.get("/guide/api/locations/catalogue")
        assert response.headers["content-type"] == "application/json"
        assert (
            next(item for item in response.json() if item["location"] == "snapshot-unsafe")["name"]
            == name
        )
        assert next(item for item in response.json() if item["location"] == "snapshot-district")[
            "context"
        ].startswith("Район · ")
