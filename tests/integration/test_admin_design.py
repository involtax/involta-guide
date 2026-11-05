"""Visual acceptance matrix, including real templates with deterministic Voice responses."""

import os
from uuid import uuid4

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
from pydantic import SecretStr

from involta_guide.main import create_app

from .editor_fixture import FixtureEditor
from .test_editorial import CONTENT
from .test_web import login
from .voice_fixture import imported


def test_all_admin_screens_share_shell_and_fit_desktop_tablet_mobile(database, monkeypatch):
    settings, _, factory, queries, city, ref, event = imported(database)
    FixtureEditor(factory).save(city["id"], CONTENT, None)
    restaurant = queries.rows("restaurants")[0]
    from involta_guide.infrastructure.voice.client import VoiceClient
    from involta_guide.infrastructure.voice.consumer import Consumer
    from involta_voice.contracts import RestaurantContext

    event["metadata"]["location"]["restaurants"] = [
        RestaurantContext(id=restaurant["id"], name=restaurant["name"]).model_dump(mode="json")
    ]
    event.update(
        event_id=str(uuid4()),
        sequence=2,
        kind="analysis_ready",
        analysis={
            "outcome": "review",
            "entries": [],
            "suggestions": [
                {
                    "kind": "edit",
                    "restaurantId": restaurant["id"],
                    "quote": "Телефон другой.",
                    "reason": "Проверить телефон",
                    "changes": {"phone": "+74951234568"},
                }
            ],
        },
    )
    Consumer(database[1], VoiceClient(settings)).import_event(event)

    class FakeVoice:
        def request(self, method, path, **kwargs):
            import httpx

            from .test_voice_service import recording

            return httpx.Response(200, content=recording(), headers={"content-type": "audio/wav"})

        def json(self, method, path, **kwargs):
            pair = [
                {"name": name, "text": "Распознанный текст " + name, "state": "completed"}
                for name in ("GigaAM", "Parakeet")
            ]
            if path == "/admin/recognitions":
                return {reference: pair for reference in kwargs["json"]}
            return {"pair": pair, "stages": []}

    from involta_guide.web.routes import voice_admin

    monkeypatch.setattr(voice_admin, "voice_client", lambda request: FakeVoice())
    settings = settings.model_copy(
        update={"voice_service_secret": SecretStr("fixture-voice-secret-" + "x" * 32)}
    )
    # An intentionally long saved response checks wrapping and the single document scroll in logs.
    with factory() as tx:
        job = {
            "id": str(uuid4()),
            "kind": "generate",
            "location_id": city["id"],
            "status": "failed",
            "error": "Тестовая ошибка",
        }
        tx.repo.queue(job)
        tx.repo.audit("generation_run", job["id"], '{"public_log": true}')
        from involta_guide.infrastructure.ai.audit import AIAudit

        log = tx.repo.log_reference(job["id"])
    AIAudit(database[1], settings).record(job["id"], "response", {"body": "длинныйответ" * 150})
    routes = {
        "overview": "/admin",
        "locations": "/admin/locations",
        "city": "/admin/locations/ivanovo",
        "new-city": "/admin/locations/new",
        "restaurants": "/admin/restaurants/ivanovo",
        "restaurant": "/admin/restaurants/" + str(restaurant["number"]),
        "filling": "/admin/settings/filling",
        "voice": "/admin/reviews",
        "voice-review": "/admin/reviews/ivanovo/1",
        "voice-log": "/logs/reviews/ivanovo/1",
        "ratings": "/admin/ratings",
        "contacts": "/admin/contacts",
        "sessions": "/admin/sessions",
        "logs": "/logs",
        "city-logs": "/logs/ivanovo",
        "log": log["path"],
    }
    from pathlib import Path

    shots = Path("var/screenshots/admin-design")
    shots.mkdir(parents=True, exist_ok=True)
    with TestClient(create_app(settings)) as client, sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        busy_actions = []

        def respond(route):
            headers = {
                key: value
                for key, value in route.request.headers.items()
                if key.lower()
                in ("hx-request", "hx-history-restore-request", "origin", "content-type")
            }
            if route.request.method == "POST":
                busy_actions.append(page.locator('button[aria-busy="true"]:disabled').count())
            response = client.request(
                route.request.method,
                route.request.url,
                headers=headers,
                content=route.request.post_data,
            )
            route.fulfill(
                status=response.status_code, body=response.content, headers=dict(response.headers)
            )

        page.route("**/*", respond)
        page.goto("http://testserver/guide/admin")
        assert page.locator(".admin-workspace").count() == 1
        assert page.locator(".admin-tabs").count() == 0
        page.screenshot(path=str(shots / "login.png"), full_page=True)
        page.goto("http://testserver/guide/logs")
        assert page.get_by_role("heading", name="Логи", exact=True).count() == 1
        assert page.locator(".admin-tabs").count() == 0
        assert "admin.css" not in client.get("/guide/ivanovo").text
        login(client)
        baseline = None
        for width in [1440, 768, 390]:
            page.set_viewport_size({"width": width, "height": 1000})
            for name, path in routes.items():
                response = client.get("/guide" + path)
                assert response.status_code == 200, (name, response.text[:200])
                page.goto("http://testserver/guide" + path, wait_until="networkidle")
                assert page.locator(".admin-workspace").count() == 1, name
                assert page.locator(".admin-tabs").count() == 1, name
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
                    name,
                    width,
                )
                box = page.locator(".admin-tabs").bounding_box()
                if name == "overview":
                    baseline = box
                assert (
                    abs(box["x"] - baseline["x"]) < 1 and abs(box["width"] - baseline["width"]) < 1
                ), (name, width)
                assert page.get_by_role("heading", name="Рабочий стол.").count() == 0, name
                assert page.locator(".header .admin-logout").count() == 1, name
                assert page.locator(".header-note").count() == 0, name
                if width != 768:
                    page.screenshot(path=str(shots / f"{name}-{width}.png"), full_page=True)
                    if name in ("city", "voice-review"):
                        page.screenshot(path=str(shots / f"{name}-viewport-{width}.png"))
                    if name == "city":
                        page.locator('form[action$="/reset-and-fill"]').locator("..").screenshot(
                            path=str(shots / f"city-reset-{width}.png")
                        )
                if name == "voice-review":
                    assert page.locator(".review-city.selected").count() == 1
                    assert (
                        page.locator(".review-transcripts > div").count()
                        == page.locator(".review-card").count()
                    )
                    assert page.locator(".review-top audio").count() == 1
                    assert (
                        page.locator(
                            ".review-feed textarea, .review-feed select, .review-feed details"
                        ).count()
                        == 0
                    )
                    audio = page.locator(".review-top audio").first.bounding_box()
                    label = page.locator(
                        ".review-top:has(audio) .review-label"
                    ).first.bounding_box()
                    assert (
                        audio["y"] < label["y"] + label["height"]
                        and audio["y"] + audio["height"] > label["y"]
                    )
                    assert (
                        page.get_by_role("navigation", name="Администрирование")
                        .get_by_role("link", name="Отзывы", exact=True)
                        .count()
                        == 1
                    )
                    assert "?tab=" not in page.locator(".admin-tabs").inner_html()
                if name == "log":
                    assert (
                        page.locator(".logs-content").evaluate(
                            "(el)=>getComputedStyle(el).overflowY"
                        )
                        == "visible"
                    )
        assert not errors
        # The stylesheet must survive both fragment navigation and history reloads.
        page.goto("http://testserver/guide/admin")
        page.get_by_role("link", name="Настройки наполнения", exact=True).click()
        page.wait_for_url("**/admin/settings/filling")
        assert page.locator(".admin-workspace").count() == 1
        page.get_by_role("button", name="Сохранить настройки наполнения", exact=True).click()
        page.wait_for_url("**/admin/settings/filling?message=**")
        assert busy_actions == [1]
        assert page.locator('button[aria-busy="true"]').count() == 0
        page.goto("http://testserver/guide/admin")
        page.get_by_role("link", name="Настройки наполнения", exact=True).click()
        page.wait_for_url("**/admin/settings/filling")
        page.go_back()
        page.wait_for_url("**/admin")
        assert page.locator(".admin-workspace").count() == 1
        browser.close()
