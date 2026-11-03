from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from involta_guide.domain.entities import Conflict
from involta_guide.main import create_app

from .test_editorial import CONTENT, services
from .test_web import login


def test_intro_updates_only_text_and_preserves_current_restaurant_data(database):
    editor, queries, _ = services(database)
    city = queries.directory(query="Иваново")["rows"][0]
    key = city["id"]
    public_version = editor.save(key, deepcopy(CONTENT), None)
    editor.publish(key, public_version, None)
    original = deepcopy(queries.get("versions", public_version)["content"])
    pending = deepcopy(original)
    pending["restaurants"][0]["description"] = "Неопубликованная правка ресторана"
    pending["restaurants"][0]["phone"] = ""
    draft_version = editor.save(key, pending, public_version)
    record_before = queries.get("restaurants", original["restaurants"][0]["id"])
    assert editor.save_intro(key, "Новое вступление", draft_version, draft_version)
    current = queries.get("locations", key)
    public = queries.public(city["slug"])["content"]
    draft = queries.location(key)["content"]
    assert public["intro"] == draft["intro"] == "Новое вступление"
    assert public["restaurants"] == pending["restaurants"]
    assert draft["restaurants"] == pending["restaurants"]
    assert current["draft_version"] == current["published_version"]
    assert queries.get("restaurants", record_before["id"]) == record_before
    assert queries.get("versions", public_version)["content"] == original
    assert queries.get("versions", draft_version)["content"] == pending


def test_intro_updates_aligned_versions_and_rejects_stale_editor(database):
    editor, queries, _ = services(database)
    city = queries.directory(query="Иваново")["rows"][0]
    key = city["id"]
    version = editor.save(key, deepcopy(CONTENT), None)
    editor.publish(key, version, None)
    editor.save_intro(key, "Опубликованный текст", version, version)
    current = queries.get("locations", key)
    assert current["draft_version"] == current["published_version"] != version
    with pytest.raises(Conflict):
        editor.save_intro(key, "Старая вкладка", version, version)
    with pytest.raises(Conflict):
        editor.save_intro(key, "Устаревшая публикация", current["draft_version"], version)
    assert queries.public(city["slug"])["content"]["intro"] == "Опубликованный текст"


def test_location_sidebar_and_intro_form_publish_independently(database):
    settings, _ = database
    editor, queries, _ = services(database)
    city = queries.directory(query="Иваново")["rows"][0]
    empty = queries.directory(query="Казань")["rows"][0]
    editor.save_intro(empty["id"], "Только черновик вступления", None, None)
    version = editor.save(city["id"], deepcopy(CONTENT), None)
    editor.publish(city["id"], version, None)
    assert [row["id"] for row in queries.ready_locations()] == [city["id"]]
    assert queries.ready_locations("Казань") == []
    with TestClient(create_app(settings)) as client:
        csrf = login(client)
        page = client.get("/guide/admin?tab=locations")
        assert page.status_code == 200 and 'id="location-intro-form"' in page.text
        assert "Готовые города" in page.text and "Настройки города" in page.text
        assert "Казань" not in page.text
        response = client.post(
            "/guide/admin/actions/intro",
            data={
                "_csrf": csrf,
                "location_id": city["id"],
                "expected": version,
                "expected_published": version,
                "intro": "Сохранено отдельной кнопкой",
            },
            headers={"origin": "http://testserver"},
        )
        assert (
            response.status_code == 200
            and "Вступление сохранено и обновлено на сайте" in response.text
        )
        assert "Сохранено отдельной кнопкой" in client.get("/guide/ivanovo").text
        stale = client.post(
            "/guide/admin/actions/intro",
            data={
                "_csrf": csrf,
                "location_id": city["id"],
                "expected": version,
                "expected_published": version,
                "intro": "Не потерять набранный текст",
            },
            headers={"origin": "http://testserver"},
        )
        assert "Вступление не сохранено" in stale.text
        assert ">Не потерять набранный текст</textarea>" in stale.text
        assert "Не потерять набранный текст" not in client.get("/guide/ivanovo").text
        assert (
            client.post(
                "/guide/admin/actions/intro",
                data={"location_id": city["id"]},
                headers={"origin": "http://testserver"},
            ).status_code
            == 403
        )
        current = queries.get("locations", city["id"])
        client.post(
            "/guide/admin/actions/content",
            data={
                "_csrf": csrf,
                "location_id": city["id"],
                "expected": current["draft_version"],
                "intro": "Ресторанная форма не должна менять вступление",
            },
            headers={"origin": "http://testserver"},
        )
        assert queries.location(city["id"])["content"]["intro"] == "Сохранено отдельной кнопкой"
        assert client.get("/guide/admin?tab=locations&location=new").status_code == 200
        assert client.get("/guide/admin?tab=locations&q=ничего").status_code == 200


def test_location_intro_in_chromium(database, tmp_path):
    import os

    from playwright.sync_api import expect, sync_playwright

    settings, _ = database
    editor, queries, _ = services(database)
    city = queries.directory(query="Иваново")["rows"][0]
    version = editor.save(city["id"], deepcopy(CONTENT), None)
    editor.publish(city["id"], version, None)
    with TestClient(create_app(settings)) as client, sync_playwright() as p:
        login(client)
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def respond(route):
            request = route.request
            headers = {
                k: v
                for k, v in request.headers.items()
                if k in ("origin", "hx-request", "content-type")
            }
            response = client.request(
                request.method, request.url, content=request.post_data, headers=headers
            )
            route.fulfill(
                status=response.status_code,
                body=response.content,
                content_type=response.headers.get("content-type", "text/plain"),
            )

        page.route("**/*", respond)
        page.goto("http://testserver/guide/admin?tab=locations", wait_until="networkidle")
        assert (
            page.locator(".restaurant-sidebar").bounding_box()["x"]
            < page.locator(".location-editor").bounding_box()["x"]
        )
        page.get_by_role("textbox", name="Вступление", exact=True).fill("Вступление из браузера")
        page.get_by_role("button", name="Сохранить вступление", exact=True).click()
        expect(page.get_by_role("status")).to_contain_text(
            "Вступление сохранено и обновлено на сайте"
        )
        assert "Вступление из браузера" in client.get("/guide/ivanovo").text
        page.screenshot(path=tmp_path / "locations-desktop.png", full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path=tmp_path / "locations-mobile.png", full_page=True)
        page.locator("#location-city-search").fill("ничего")
        expect(page.get_by_role("heading", name="Выберите город", exact=True)).to_be_visible()
        assert not errors
        browser.close()


def test_history_restore_updates_site_and_retains_previous_snapshots(database):
    settings, _ = database
    editor, queries, _ = services(database)
    city = queries.directory(query="Иваново")["rows"][0]
    first = editor.save(city["id"], deepcopy(CONTENT), None)
    second = editor.save_intro(city["id"], "Новый актуальный текст", first, first)
    assert second
    current = queries.get("locations", city["id"])["published_version"]
    with TestClient(create_app(settings)) as client:
        path = "/guide/logs/history/" + city["id"]
        assert client.get(path).status_code == 401
        csrf = login(client)
        page = client.get(path)
        assert page.status_code == 200 and "Восстановить эти данные на сайте" in page.text
        restored = client.post(
            "/guide/admin/actions/restore",
            data={
                "_csrf": csrf,
                "location_id": city["id"],
                "version_id": first,
                "expected": current,
            },
            headers={"origin": "http://testserver"},
        )
        assert restored.status_code == 200
        assert queries.public(city["slug"])["content"]["intro"] == CONTENT["intro"]
        assert queries.get("versions", current)["content"]["intro"] == "Новый актуальный текст"


def test_transition_adopts_last_saved_state_without_deleting_history(database):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    _, engine = database
    editor, queries, _ = services(database)
    city = queries.directory(query="Иваново")["rows"][0]
    first = editor.save(city["id"], deepcopy(CONTENT), None)
    editor.save_intro(city["id"], "Последний сохранённый текст", first, first)
    last = queries.get("locations", city["id"])["published_version"]
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE locations SET published_version=:first WHERE id=:id"),
            {"first": first, "id": city["id"]},
        )
        path = Path("migrations/versions/0005_single_current_page.py")
        spec = importlib.util.spec_from_file_location("single_current_migration", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
    row = queries.get("locations", city["id"])
    assert row["published_version"] == row["draft_version"] == last
    assert queries.get("versions", first)["content"]["intro"] == CONTENT["intro"]
    assert queries.public(city["slug"])["content"]["intro"] == "Последний сохранённый текст"
