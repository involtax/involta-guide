"""Acceptance coverage for explicit admin routes and atomic manual intro edits."""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_guide.application.services.locations import LocationService
from involta_guide.domain.entities import Conflict
from involta_guide.infrastructure.persistence.repositories.postgres import PostgresRepository
from involta_guide.main import create_app

from .test_editorial import CONTENT, services
from .test_public_fill import setup
from .test_web import login

HEADERS = {"origin": "http://testserver"}


def test_routes_settings_filters_auth_and_old_addresses(database):
    settings, _ = database
    _, queries, _ = services(database)
    paths = [
        "/admin/locations",
        "/admin/locations/new",
        "/admin/locations/export",
        "/admin/locations/ivanovo",
        "/admin/restaurants",
        "/admin/restaurants/ivanovo",
        "/admin/ratings",
        "/admin/contacts",
        "/admin/settings/filling",
        "/admin/sessions",
    ]
    with TestClient(create_app(settings)) as client:
        for path in paths:
            assert client.get("/guide" + path).status_code == 401
        csrf = login(client)
        for path in paths:
            assert client.get("/guide" + path).status_code == 200
        for tab in (
            "overview",
            "cities",
            "locations",
            "fill",
            "restaurants",
            "sessions",
            "ai-audit",
        ):
            response = client.get("/guide/admin?tab=" + tab, follow_redirects=False)
            assert response.status_code == 200 and "Рабочий стол." in response.text
            assert 'href="/guide/admin" class="selected"' in response.text
            assert "location" not in response.headers and "HX-Redirect" not in response.headers
        for query in (
            "tab=reviews&review=broken",
            "city=old&location=broken",
            "anything=123&page=bad",
        ):
            response = client.get("/guide/admin?" + query, headers={"HX-Request": "true"})
            assert response.status_code == 200 and "Рабочий стол." in response.text
            assert "HX-Redirect" not in response.headers
        for old in (
            "/admin/voice",
            "/admin/voice/ivanovo/1",
            "/admin/voice/ivanovo/1/runs/2",
            "/admin/voice/legacy/" + str(uuid4()),
            "/admin/voice/submission/" + str(uuid4()),
            "/admin/voice/media/audio/" + str(uuid4()),
            "/api/admin/audio/" + str(uuid4()),
            "/admin/jobs",
            "/admin/api/locations-export",
            "/api/admin/locations-export",
            "/admin/ai-audit/" + str(uuid4()) + ".json",
        ):
            assert client.get("/guide" + old, follow_redirects=False).status_code == 404
        assert client.get("/guide/admin/locations?location=" + str(uuid4())).status_code == 200
        assert client.get("/guide/admin/restaurants/" + str(uuid4())).status_code == 404
        for old in (
            "/admin/voice/settings",
            "/admin/voice/decision/" + str(uuid4()),
            "/admin/voice/submission/" + str(uuid4()),
        ):
            assert (
                client.post("/guide" + old, data={"_csrf": csrf}, headers=HEADERS).status_code
                == 404
            )
        assert client.get("/guide/admin/restaurants?location=missing").status_code == 200
        assert "Иваново" in client.get("/guide/admin/locations?status=empty&q=Иваново").text
        assert "Выберите город" in client.get("/guide/admin/locations?status=filled").text
        for action in ("location", "prompt", "generation-model", "generate", "cancel", "attach"):
            assert (
                client.post(
                    "/guide/admin/actions/" + action, data={"_csrf": csrf}, headers=HEADERS
                ).status_code
                == 404
            )
        prompt = "Новый промпт будущего наполнения. " * 3
        assert (
            client.post(
                "/guide/admin/settings/filling",
                data={"_csrf": csrf, "prompt": prompt, "model": "gpt-5"},
                headers=HEADERS,
            ).status_code
            == 200
        )
        assert queries.setting("prompt") == prompt
        assert queries.setting("generation_model") == "gpt-5"
        assert queries.rows("jobs") == []
        assert (
            client.post(
                "/guide/admin/settings/filling",
                data={"_csrf": csrf, "prompt": "bad", "model": "gpt-4.1"},
                headers=HEADERS,
            ).status_code
            == 200
        )
        assert (
            queries.setting("prompt") == prompt and queries.setting("generation_model") == "gpt-5"
        )
        for path in (
            "/admin/settings/filling",
            "/admin/locations/ivanovo/intro",
            "/admin/locations",
        ):
            assert client.post("/guide" + path, data={}, headers=HEADERS).status_code == 403


def test_intro_is_atomic_isolated_escaped_and_conflict_preserves_input(database, monkeypatch):
    settings, _ = database
    editor, queries, factory = services(database)
    city = queries.directory(query="Иваново")["rows"][0]
    revision = editor.save(city["id"], deepcopy(CONTENT), None)
    before = queries.get("locations", city["id"])
    restaurants = queries.rows("restaurants")
    with TestClient(create_app(settings)) as client:
        csrf = login(client)

        def submit(value, expected=revision):
            return client.post(
                "/guide/admin/locations/ivanovo/intro",
                data={"_csrf": csrf, "intro": value, "expected": expected or ""},
                headers=HEADERS,
            )

        assert submit("<script>alert(1)</script> Новый текст").status_code == 200
        current = queries.get("locations", city["id"])
        assert current["revision"] != revision
        assert current["initialized_at"] == before["initialized_at"]
        assert queries.rows("restaurants") == restaurants
        assert "<script>alert(1)</script>" not in client.get("/guide/ivanovo").text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in client.get("/guide/ivanovo").text
        events = queries.rows("audit", {"action": "manual_intro", "target": city["id"]})
        assert len(events) == 1
        diff = json.loads(events[0]["detail"])
        assert diff["diff"] == [
            {"path": ["intro"], "before": before["intro"], "after": current["intro"]}
        ]
        assert "token" not in events[0]["detail"]
        assert submit(current["intro"], current["revision"]).status_code == 200
        assert queries.get("locations", city["id"])["revision"] == current["revision"]
        assert len(queries.rows("audit", {"action": "manual_intro"})) == 1
        conflict = submit("Мой несохранённый текст")
        assert conflict.status_code == 409 and "Мой несохранённый текст" in conflict.text
        assert "Сравните тексты" in conflict.text
        assert queries.get("locations", city["id"]) == current
        assert submit("x" * 30001, current["revision"]).status_code == 400
        assert "Журнал ручных правок" in client.get("/guide/admin/locations/ivanovo").text
        assert diff["admin_session_id"] not in client.get("/guide/ivanovo").text
    original = PostgresRepository.audit

    def broken_audit(repo, action, target, detail=""):
        if action == "manual_intro":
            raise RuntimeError("Simulate audit failure")
        return original(repo, action, target, detail)

    monkeypatch.setattr(PostgresRepository, "audit", broken_audit)
    with pytest.raises(RuntimeError):
        LocationService(factory).save_intro(
            city["id"], "rollback", current["revision"], str(uuid4())
        )
    assert queries.get("locations", city["id"]) == current


def test_concurrent_intro_edits_have_one_winner(database):
    _, queries, factory = services(database)
    city = queries.directory(query="Иваново")["rows"][0]
    service = LocationService(factory)

    def edit(value):
        try:
            return service.save_intro(city["id"], value, city["revision"], str(uuid4()))
        except Conflict:
            return False

    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(edit, ["Первый текст", "Второй текст"])) == 1
    assert len(queries.rows("audit", {"action": "manual_intro"})) == 1


def test_pending_generation_cannot_overwrite_manual_intro(database):
    from involta_guide.infrastructure.jobs.worker import Worker

    _, _, factory, queries, city, service, queue = setup(database)
    service.enqueue_public_fill(city["id"])
    LocationService(factory).save_intro(
        city["id"], "Вручную после запуска", city["revision"], str(uuid4())
    )
    Worker(queue, service).once()
    assert queries.get("locations", city["id"])["intro"] == "Вручную после запуска"
    job = queries.rows("jobs")[0]
    assert job["status"] == "failed" and "изменена" in job["error"]


def test_restaurant_numbers_concurrent_stable_and_slug_updates_log_path(database):
    settings, engine = database
    editor, queries, factory = services(database)
    city = queries.directory(query="Иваново")["rows"][0]
    editor.save(city["id"], CONTENT, None)
    first = queries.rows("restaurants")[0]

    def add(n):
        with factory() as tx:
            return tx.repo.add(
                "restaurants",
                {
                    "id": str(uuid4()),
                    "location_id": city["id"],
                    "name": "Ресторан " + str(n),
                    "cuisine": "",
                    "description": "",
                    "maps_url": "https://example.com/" + str(n),
                },
            )["number"]

    with ThreadPoolExecutor(4) as pool:
        numbers = list(pool.map(add, range(25)))
    assert len(set(numbers + [first["number"]])) == 26 and min(numbers) > 0
    with factory() as tx:
        tx.repo.change("restaurants", first["id"], {"name": "Переименованный"})
        tx.repo.queue({"id": str(uuid4()), "kind": "generate", "location_id": city["id"]})
        tx.repo.change("locations", city["id"], {"slug": "ivanovo-new"})
    assert queries.get("restaurants", first["id"])["number"] == first["number"]
    with TestClient(create_app(settings)) as client:
        login(client)
        second_page = client.get("/guide/admin/restaurants/ivanovo-new?page=2&q=Иван")
        assert second_page.status_code == 200
        assert second_page.text.count('<article class="restaurant-record">') == 6
        assert 'href="/guide/admin/restaurants/ivanovo-new?page=1&amp;q=' in second_page.text
        assert "?location=" not in second_page.text
        page = client.get("/guide/admin/restaurants/" + str(first["number"]))
        assert (
            page.status_code == 200 and "Переименованный" in page.text and "Казань" not in page.text
        )
        assert "/admin/locations/ivanovo-new" in page.text
        assert 'href="/guide/ivanovo-new" target="_blank"' in page.text
        assert client.get("/guide/admin/restaurants/ivanovo").status_code == 404
        assert client.get("/guide/admin/locations/ivanovo").status_code == 404
        assert client.get("/guide/admin/locations/ivanovo-new").status_code == 200
        assert client.get("/guide/logs/ivanovo-new/1").status_code == 200
        assert client.get("/guide/logs/ivanovo/1").status_code == 404
    command.upgrade(Config(str(settings.root_dir / "alembic.ini")), "head")
    assert queries.get("restaurants", first["id"])["number"] == first["number"]
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT count(*)=count(DISTINCT number) FROM restaurants")
        ).scalar()


def test_custom_prefix_and_htmx_history_restore(database):
    settings, _ = database
    settings = settings.model_copy(
        update={"base_path": "/travel", "site_url": "http://testserver/travel"}
    )
    with TestClient(create_app(settings)) as client:
        page = client.get("/travel/admin")
        from .test_web import token

        page = client.post(
            "/travel/admin/login",
            data={"password": "test-password-123456", "_csrf": token(page.text)},
            headers=HEADERS,
        )
        assert "/travel/admin/locations" in page.text and "/guide/admin/locations" not in page.text
        fragment = client.get("/travel/admin/locations/ivanovo", headers={"HX-Request": "true"})
        assert fragment.status_code == 200 and "<!doctype" not in fragment.text
        restored = client.get(
            "/travel/admin/locations/ivanovo",
            headers={"HX-Request": "true", "HX-History-Restore-Request": "true"},
        )
        assert "<!doctype" in restored.text
        assert client.get("/travel", follow_redirects=False).headers["location"] == "/travel/"
        restaurants = client.get("/travel/admin/restaurants/ivanovo")
        assert restaurants.status_code == 200
        assert 'href="/travel/ivanovo" target="_blank"' in restaurants.text
        assert "/guide/ivanovo" not in restaurants.text


def test_location_create_settings_slug_validation_and_relationships(database):
    settings, _ = database
    _, queries, _ = services(database)
    parent = queries.directory(query="Казань")["rows"][0]
    related = queries.directory(query="Иваново")["rows"][0]
    with TestClient(create_app(settings)) as client:
        csrf = login(client)
        data = {
            "_csrf": csrf,
            "name": "Новый город",
            "region": "Область",
            "slug": "admin-new-city",
            "in_phrase": "в новом городе",
            "kind": "city",
            "priority": "medium",
            "top_limit": "4",
            "active": "on",
            "listed": "on",
            "parent_id": parent["id"],
            "related_ids": related["id"],
        }
        response = client.post("/guide/admin/locations?slug=ivanovo", data=data, headers=HEADERS)
        assert (
            response.status_code == 200
            and response.url.path == "/guide/admin/locations/admin-new-city"
        )
        city = queries.admin_location("admin-new-city")["location"]
        assert city["parent_id"] == parent["id"] and city["top_limit"] == 4
        assert queries.admin_location(city["slug"])["related_ids"] == [related["id"]]
        page = client.get("/guide/admin/locations/admin-new-city")
        assert "Родительская локация (UUID)" not in page.text and "Казань · kazan" in page.text
        data["slug"] = "admin-new-city-renamed"
        response = client.post("/guide/admin/locations/admin-new-city", data=data, headers=HEADERS)
        assert response.url.path.endswith("/admin-new-city-renamed")
        assert client.get("/guide/admin/locations/admin-new-city").status_code == 404
        assert any(
            c["slug"] == data["slug"] for c in client.get("/guide/admin/locations/export").json()
        )
        data["slug"] = "ivanovo"
        response = client.post(
            "/guide/admin/locations/admin-new-city-renamed", data=data, headers=HEADERS
        )
        assert "URL уже занят" in response.text
        assert queries.get("locations", city["id"])["slug"] == "admin-new-city-renamed"
        data["slug"] = "export"
        assert (
            "URL зарезервирован"
            in client.post("/guide/admin/locations", data=data, headers=HEADERS).text
        )
        data["name"] = ""
        assert client.post("/guide/admin/locations", data=data, headers=HEADERS).status_code == 200
