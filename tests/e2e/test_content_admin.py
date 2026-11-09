"""Mobile-first touch acceptance of the independent content controls, without model calls."""

import os
from pathlib import Path

from fastapi.testclient import TestClient
from integration.test_incremental_content import discovered, imported, research
from integration.test_web import login
from playwright.sync_api import expect, sync_playwright

from involta_guide.main import create_app


def test_independent_content_controls_mobile_then_desktop(database):
    settings, _, _, queries, city, jobs, _, content, restaurant = discovered(database)
    imported(content, city["id"], "research", research(), restaurant["id"])
    imported(
        content, city["id"], "intro", {"intro": "Вступление города перед независимым обновлением."}
    )
    shots = Path("var/screenshots/incremental-content")
    shots.mkdir(parents=True, exist_ok=True)
    with TestClient(create_app(settings)) as client, sync_playwright() as p:
        login(client)
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        for width in (320, 390, 1440):
            page = browser.new_page(
                viewport={"width": width, "height": 920},
                is_mobile=width < 500,
                has_touch=width < 500,
            )

            def respond(route):
                headers = {
                    key: value
                    for key, value in route.request.headers.items()
                    if key in ("hx-request", "origin", "content-type")
                }
                response = client.request(
                    route.request.method,
                    route.request.url,
                    headers=headers,
                    content=route.request.post_data,
                )
                route.fulfill(
                    status=response.status_code,
                    body=response.content,
                    headers=dict(response.headers),
                )

            page.route("**/*", respond)
            page.goto("http://testserver/guide/admin/locations/" + city["slug"])
            expect(
                page.get_by_role("button", name="Написать вступление", exact=True)
            ).to_be_visible()
            expect(page.get_by_role("button", name="Найти ещё места", exact=True)).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            page.locator(".location-intro-panel").screenshot(path=str(shots / f"intro-{width}.png"))
            page.locator(".content-actions").locator("..").screenshot(
                path=str(shots / f"city-actions-{width}.png")
            )
            page.goto("http://testserver/guide/admin/restaurants/" + city["slug"])
            expect(page.get_by_role("button", name="Переоценить", exact=True)).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            page.locator(".restaurant-record").screenshot(
                path=str(shots / f"restaurant-{width}.png")
            )
            page.get_by_role("button", name="Скрыть", exact=True).click()
            expect(page.get_by_role("button", name="Показать", exact=True)).to_be_visible()
            assert queries.get("restaurants", restaurant["id"])["active"] == 0
            page.get_by_role("button", name="Показать", exact=True).click()
            expect(page.get_by_role("button", name="Скрыть", exact=True)).to_be_visible()
            assert queries.get("restaurants", restaurant["id"])["active"] == 1
            page.goto("http://testserver/guide/admin/settings/filling")
            assert page.locator("textarea").count() == 3
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            page.screenshot(path=str(shots / f"settings-{width}.png"), full_page=True)
            page.close()
        page = browser.new_page(
            viewport={"width": 390, "height": 920}, is_mobile=True, has_touch=True
        )
        page.route("**/*", respond)
        page.goto("http://testserver/guide/admin/restaurants/" + city["slug"])
        page.get_by_role("button", name="Переоценить", exact=True).click()
        expect(page).to_have_url(__import__("re").compile(r"/guide/logs/ivanovo/\d+$"))
        queued = [j for j in queries.rows("jobs") if j["status"] == "queued"]
        assert len(queued) == 1 and queued[0]["report"]["scope"] == "assessment"
        assert queued[0]["report"]["restaurant_id"] == restaurant["id"]
        page.goto("http://testserver/guide/admin/restaurants/" + city["slug"])
        expect(page.get_by_role("button", name="Переоценить", exact=True)).to_be_disabled()
        assert jobs.provider.calls == []
        browser.close()
