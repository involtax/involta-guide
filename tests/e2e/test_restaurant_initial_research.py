"""A placeholder has one explicit first-research action; a real card can be reassessed."""

import os
import re
from pathlib import Path

from fastapi.testclient import TestClient
from integration.test_incremental_content import discovered
from integration.test_web import login
from playwright.sync_api import expect, sync_playwright

from involta_guide.infrastructure.jobs.worker import Worker
from involta_guide.main import create_app


def test_first_research_and_reassessment_mobile_first(database):
    settings, _, _, queries, city, jobs, queue, _, restaurant = discovered(database)
    shots = Path("var/screenshots/restaurant-initial-research")
    shots.mkdir(parents=True, exist_ok=True)
    with TestClient(create_app(settings)) as client, sync_playwright() as p:
        login(client)
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )

        def respond(route):
            headers = {
                k: v
                for k, v in route.request.headers.items()
                if k in ("hx-request", "origin", "content-type")
            }
            response = client.request(
                route.request.method,
                route.request.url,
                headers=headers,
                content=route.request.post_data,
            )
            route.fulfill(
                status=response.status_code, body=response.content, headers=dict(response.headers)
            )

        for state in ("placeholder", "complete"):
            for width in (320, 390, 1440):
                page = browser.new_page(
                    viewport={"width": width, "height": 920},
                    is_mobile=width < 500,
                    has_touch=width < 500,
                )
                page.route("**/*", respond)
                page.goto("http://testserver/guide/admin/restaurants/" + city["slug"])
                if state == "placeholder":
                    expect(
                        page.get_by_role("button", name="Заполнить и оценить", exact=True)
                    ).to_be_visible()
                    expect(page.locator('form[action$="/content/assessment"]')).to_have_count(0)
                    expect(page.locator('form[action$="/content/card"]')).to_have_count(0)
                else:
                    expect(
                        page.get_by_role("button", name="Обновить карточку", exact=True)
                    ).to_be_visible()
                    expect(
                        page.get_by_role("button", name="Переоценить", exact=True)
                    ).to_be_visible()
                    expect(
                        page.get_by_role("button", name="Заполнить и оценить", exact=True)
                    ).to_have_count(0)
                assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
                page.locator(".restaurant-record").screenshot(
                    path=str(shots / f"{state}-{width}.png")
                )
                page.close()
            if state == "placeholder":
                page = browser.new_page(
                    viewport={"width": 390, "height": 920}, is_mobile=True, has_touch=True
                )
                page.route("**/*", respond)
                page.goto("http://testserver/guide/admin/restaurants/" + city["slug"])
                page.get_by_role("button", name="Заполнить и оценить", exact=True).click()
                expect(page).to_have_url(re.compile(r"/guide/logs/ivanovo/\d+$"))
                queued = [j for j in queries.rows("jobs") if j["status"] == "queued"]
                assert len(queued) == 1 and queued[0]["report"]["scope"] == "all"
                assert Worker(queue, jobs).once()
                assert queries.get("restaurants", restaurant["id"])["active"] == 1
                page.close()
        assert len(jobs.provider.calls) == 1  # One fake full research, no actual paid calls.
        browser.close()
