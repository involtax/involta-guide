import re
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import event, text

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
            assert client.get("/guide/", params={"q": "x" * 101}).status_code == 422
    finally:
        event.remove(search_engine, "before_cursor_execute", capture)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM jobs")).scalar_one() == 0


def test_native_search_ranks_only_names_with_short_prefix_and_three_letter_substring(database):
    settings, engine = database
    add_location(engine, "Йцук", "name-exact")
    add_location(engine, "Йцукоград", "name-prefix", ready=True)
    add_location(engine, "Старый Йцукоград", "name-later-word", ready=True)
    add_location(engine, "Айцукоград", "name-inside-word", ready=True)
    add_location(engine, "Региональный", "name-region", region="Йцук", ready=True)
    add_location(engine, "Алиасный", "name-alias", aliases=["Йцук"], ready=True)
    add_location(
        engine,
        "Иной родитель",
        "name-parent",
        parent=add_location(engine, "Йцук родитель", "name-parent-title"),
    )
    add_location(engine, "Другой", "йцук-slug")
    with TestClient(create_app(settings)) as client:
        for query in ("й", "ЙЦ", "йцу"):
            page = client.get("/guide/", params={"q": query})
            urls = re.findall(r'<a class="city-link" href="([^"]+)"', page.text)
            assert "/guide/name-exact" in urls and "/guide/name-prefix" in urls
            if len(query) < 3:
                assert (
                    "/guide/name-later-word" not in urls and "/guide/name-inside-word" not in urls
                )
            else:
                assert "/guide/name-later-word" in urls and "/guide/name-inside-word" in urls
            for excluded in ("region", "alias", "parent"):
                assert f"/guide/name-{excluded}" not in urls
            assert "/guide/йцук-slug" not in urls
        exact = client.get("/guide/", params={"q": "йцук"})
        assert (
            re.findall(r'<a class="city-link" href="([^"]+)"', exact.text)[0] == "/guide/name-exact"
        )


def test_real_catalogue_name_search_normalization_keyboard_typo_and_empty(database):
    settings, _ = database
    with TestClient(create_app(settings)) as client:
        for query, expected in [
            ("ПлЕс", "Плёс"),
            ("новгород нижний", "Нижний Новгород"),
            ("cjxb", "Сочи"),
            ("Сочии", "Сочи"),
        ]:
            page = client.get("/guide/", params={"q": query})
            assert re.findall(r"<strong>(.*?)</strong>", page.text)[0] == expected
        for query in ("sochi", "жцфыщбъь", "%_"):
            page = client.get("/guide/", params={"q": query})
            assert 'class="city-link"' not in page.text


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
