from copy import deepcopy
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_guide.application.services.locations import Queries
from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow
from involta_guide.main import create_app

from .editor_fixture import FixtureEditor
from .test_editorial import CONTENT
from .test_location_search import add_location
from .test_web import login

HEADERS = {"origin": "http://testserver"}


def public_restaurant(settings, engine, name="Пушкин"):
    def factory():
        return PostgresUow(engine)

    city = Queries(factory).directory(query="Иваново")["rows"][0]
    content = deepcopy(CONTENT)
    content["restaurants"][0]["name"] = name
    FixtureEditor(factory).save(city["id"], content, None)
    with engine.connect() as connection:
        card = dict(
            connection.execute(
                text("SELECT * FROM restaurants WHERE location_id=:id"), {"id": city["id"]}
            )
            .mappings()
            .one()
        )
    return city, card


def find(client, query, token=None):
    token = token or str(uuid4())
    response = client.post(
        "/guide/api/search", json={"query": query, "token": token}, headers=HEADERS
    )
    assert response.status_code == 200, response.text
    return token, response.json()["results"]


def shown(rows):
    return [{key: value for key, value in row.items() if key != "rank"} for row in rows]


def event(client, token, query, rows, *, seq=1, reason=None, clicked_id=None, **extra):
    return client.post(
        "/guide/api/search/events",
        json=dict(
            token=token,
            query=query,
            seq=seq,
            shown=shown(rows) if rows is not None else None,
            complete=extra.pop("complete", True),
            reason=reason,
            clicked_id=clicked_id,
            **extra,
        ),
        headers=HEADERS,
    )


def test_published_names_morphology_scope_and_no_page_side_effects(database):
    settings, engine = database
    city, card = public_restaurant(settings, engine)
    add_location(engine, "Санкт-Петербург", "search-alias-spb")
    hidden = add_location(engine, "Скрытый тест", "hidden-test", listed=False)
    inactive = add_location(engine, "Выключенный тест", "inactive-test", active=0, ready=True)
    with engine.begin() as connection:
        for name, location, active in [
            ("Скрытый ресторан", hidden, 1),
            ("Выключенный ресторан", inactive, 1),
            ("Неопубликованный ресторан", city["id"], 0),
        ]:
            connection.execute(
                text(
                    "INSERT INTO restaurants(id,location_id,name,active) VALUES(:id,:location,:name,:active)"
                ),
                dict(id=str(uuid4()), location=location, name=name, active=active),
            )
    with TestClient(create_app(settings)) as client:
        _, rows = find(client, "Пушкин в Иванове")
        assert len(rows) == 1 and rows[0]["name"] == "Пушкин"
        assert rows[0]["kind"] == "restaurant" and rows[0]["context"] == "Иваново"
        assert rows[0]["anchor"] == "restaurant-" + str(card["id"])
        assert find(client, "Пушкни")[1][0]["name"] == "Пушкин"
        assert find(client, "в Иванове")[1][0]["name"] == "Иваново"
        assert find(client, "питер")[1][0]["name"] == "Санкт-Петербург"
        assert not find(client, "Скрытый ресторан")[1]
        assert not find(client, "Выключенный ресторан")[1]
        assert not find(client, "Неопубликованный ресторан")[1]
        assert client.get("/guide/api/search?q=test").status_code == 405
        assert (
            client.post(
                "/guide/api/search", json={"token": str(uuid4()), "query": "x"}, headers=HEADERS
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/guide/api/search",
                json={"token": str(uuid4()), "query": "Иваново"},
                headers={"origin": "https://evil.example"},
            ).status_code
            == 403
        )
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM jobs")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM audience_daily")).scalar_one() == 0


def test_journal_late_delivery_masked_ip_retention_and_read_only_replay(database):
    settings, engine = database
    with TestClient(create_app(settings), client=("203.0.113.57", 12345)) as client:
        token, rows = find(client, "Иваново")
        assert (
            event(
                client, token, "Иваново", rows, seq=3, reason="select", clicked_id=rows[0]["id"]
            ).status_code
            == 204
        )
        assert event(client, token, "Иваново", [], seq=1, reason="refined").status_code == 204
        find(client, "Иваново", token)
        assert event(client, token, "Иваново", None, seq=4, reason="leave").status_code == 204
        empty_token, empty_rows = find(client, "абракадабраxyz777")
        assert not empty_rows
        assert event(client, empty_token, "абракадабраxyz777", [], reason="blur").status_code == 204
        report = client.get("/guide/logs/search.json").json()
        chosen = next(row for row in report["attempts"] if row["query"] == "Иваново")
        assert chosen["outcome"] == "selected" and chosen["clicked_position"] == 1
        assert chosen["ip"] == "203.0.113.0/24"
        assert token not in str(report) and "203.0.113.57" not in str(report)
        assert report["totals"]["searches"] == 2 and report["totals"]["empty"] == 1
        assert (
            client.get("/guide/logs/search.json?outcome=no_click").json()["attempts"][0]["query"]
            == "абракадабраxyz777"
        )
        assert (
            client.get("/guide/logs/search/replay", params={"text": "в Иванове"}).json()["results"][
                0
            ]["name"]
            == "Иваново"
        )
        assert client.get("/guide/logs/search").status_code == 200
        with engine.begin() as connection:
            assert (
                connection.execute(
                    text("SELECT host(ip) FROM search_attempts WHERE token=:token"),
                    {"token": token},
                ).scalar_one()
                == "203.0.113.57"
            )
            connection.execute(
                text("UPDATE search_attempts SET created_at=now()-interval '400 days'")
            )
            assert (
                connection.execute(text("SELECT count(*) FROM search_attempts")).scalar_one() == 2
            )
        assert client.get("/guide/logs/search.json").json()["totals"]["searches"] == 2
        settings.public_fill_logs_enabled = False
        assert client.get("/guide/logs/search.json").status_code == 404
        assert client.get("/guide/logs/search/replay?text=Иваново").status_code == 404
        login(client)
        assert client.get("/guide/logs/search.json").status_code == 200


def test_local_click_before_search_admin_exclusion_and_untrusted_forwarded_ip(database):
    settings, engine = database
    result = dict(
        id="location:ivanovo",
        kind="location",
        name="Иваново",
        location="ivanovo",
        context="Ивановская область",
        anchor="",
        match="local",
    )
    with TestClient(create_app(settings), client=("203.0.113.57", 12345)) as client:
        token = str(uuid4())
        client.headers["x-forwarded-for"] = "192.0.2.99"
        assert (
            event(
                client, token, "и", [result], reason="select", clicked_id=result["id"]
            ).status_code
            == 204
        )
        report = client.get("/guide/logs/search.json").json()
        assert report["totals"]["selected"] == 1
        assert report["attempts"][0]["server_state"] == "not_requested"
        settings.trusted_proxy_ips = "203.0.113.57"
        client.headers["x-forwarded-for"] = "2001:db8:abcd::1234"
        login(client)
        admin_token, _ = find(client, "Плёс")
        assert client.get("/guide/logs/search.json").json()["totals"]["searches"] == 1
        admin_rows = client.get("/guide/logs/search.json?include_admin=true").json()["attempts"]
        assert admin_rows[0]["is_admin"] and admin_rows[0]["ip"] == "2001:db8:abcd::/48"
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT host(ip) FROM search_attempts WHERE token=:token"),
                    {"token": token},
                ).scalar_one()
                == "203.0.113.57"
            )
            assert (
                connection.execute(
                    text("SELECT host(ip) FROM search_attempts WHERE token=:token"),
                    {"token": admin_token},
                ).scalar_one()
                == "2001:db8:abcd::1234"
            )


def test_shown_empty_requires_completed_search_and_json_html_are_safe(database):
    settings, engine = database
    with TestClient(create_app(settings)) as client:
        token = str(uuid4())
        raw = '<script>alert("search")</script>'
        assert event(client, token, raw, [], complete=False).status_code == 204
        report = client.get("/guide/logs/search.json").json()
        assert report["totals"]["empty"] == 0
        assert event(client, token, raw, [], seq=2, reason="refined").status_code == 204
        html = client.get("/guide/logs/search").text
        assert "&lt;script&gt;" in html and raw not in html
        assert "noindex" in html
        assert client.get("/guide/logs/search.json?limit=201").status_code == 422
        assert (
            event(
                client, token, raw, [], seq=3, reason="select", clicked_id="restaurant:missing"
            ).status_code
            == 422
        )


def test_server_error_is_not_empty_and_journal_failure_does_not_break_search(
    database, monkeypatch, caplog
):
    from involta_guide.web.routes import search as routes

    settings, engine = database
    original = routes.catalogue

    def unavailable(_):
        raise RuntimeError("Controlled search failure")

    with TestClient(create_app(settings)) as client:
        monkeypatch.setattr(routes, "catalogue", unavailable)
        response = client.post(
            "/guide/api/search", json={"query": "Иваново", "token": str(uuid4())}, headers=HEADERS
        )
        assert response.status_code == 503
        report = client.get("/guide/logs/search.json").json()
        assert report["totals"]["errors"] == 1 and report["totals"]["empty"] == 0
        assert report["attempts"][0]["outcome"] == "error"
        monkeypatch.setattr(routes, "catalogue", original)
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE search_attempts"))
        _, rows = find(client, "Иваново")
        assert rows[0]["name"] == "Иваново"
        assert any(
            record.message == "Could not store search diagnostics" for record in caplog.records
        )
