import json
import re

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


def snapshot(response):
    return json.loads(
        re.search(
            r'<script type="application/json" id="location-catalogue">(.*?)</script>',
            response.text,
            re.S,
        )[1]
    )


def test_public_snapshot_is_one_select_public_fields_only_and_fresh_on_reload(database):
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
            response = client.get("/guide/")
            items = snapshot(response)
            urls = {item["url"] for item in items}
            assert len(selects) == 1
            assert all(set(item) == {"name", "context", "url"} for item in items)
            assert {
                "/guide/snapshot-open",
                "/guide/snapshot-parent",
                "/guide/snapshot-child",
            } <= urls
            assert "/guide/snapshot-hidden" not in urls and "/guide/snapshot-inactive" not in urls
            with engine.begin() as connection:
                connection.execute(
                    text("UPDATE locations SET name=:name WHERE id=:id"),
                    {"name": "Новое Название", "id": key},
                )
            updated = snapshot(client.get("/guide/"))
            assert (
                next(item for item in updated if item["url"] == "/guide/snapshot-open")["name"]
                == "Новое Название"
            )
            assert len(selects) == 2
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


def test_inline_snapshot_escapes_script_closing_tags_and_keeps_exact_name(database):
    settings, engine = database
    name = '</script><img src=x onerror="window.__searchXss=true">'
    add_location(engine, name, "snapshot-unsafe")
    with TestClient(create_app(settings)) as client:
        page = client.get("/guide/")
        assert name not in page.text
        assert (
            next(item for item in snapshot(page) if item["url"] == "/guide/snapshot-unsafe")["name"]
            == name
        )
