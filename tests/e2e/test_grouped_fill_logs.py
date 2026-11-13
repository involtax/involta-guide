"""A single launch on mobile, live discovery children, unchanged detail links."""

import os
from pathlib import Path

from fastapi.testclient import TestClient
from integration.test_incremental_content import ContentAI
from integration.test_jobs import setup
from playwright.sync_api import expect, sync_playwright

from involta_guide.infrastructure.jobs.worker import Worker
from involta_guide.main import create_app


def test_grouped_fill_mobile_first_and_live_children(database):
    settings, _, _, queries, city, jobs, queue = setup(database, ContentAI())
    jobs.enqueue_public_fill(city["id"])
    shots = Path("var/screenshots/grouped-fill-logs")
    shots.mkdir(parents=True, exist_ok=True)
    with TestClient(create_app(settings)) as client, sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )

        def respond(route):
            response = client.request(route.request.method, route.request.url)
            route.fulfill(
                status=response.status_code, body=response.content, headers=dict(response.headers)
            )

        for width in (320, 390, 1440):
            page = browser.new_page(
                viewport={"width": width, "height": 920},
                is_mobile=width < 500,
                has_touch=width < 500,
            )
            page.route("**/*", respond)
            page.clock.install()
            page.goto("http://testserver/guide/logs?q=Иваново")
            expect(page.locator(".logs-sidebar .log-request")).to_have_count(1)
            if width == 320:
                expect(page.locator(".log-group-requests li")).to_have_count(2)
                worker = Worker(queue, jobs)
                assert worker.once() and worker.once() and worker.once()
                page.clock.fast_forward(10001)
            expect(page.locator(".log-group-requests li")).to_have_count(3)
            page.locator(".log-group-requests a").filter(has_text="Исследование").click()
            expect(page).to_have_url(
                __import__("re").compile(r"/guide/logs/ivanovo/3\?.*#selected-request")
            )
            expect(page.locator('.logs-sidebar .log-request[aria-current="page"]')).to_have_count(1)
            expect(page.get_by_role("link", name="Открыть полный JSON ↗")).to_have_attribute(
                "href", "/guide/logs/ivanovo/3.json"
            )
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            page.locator(".log-group").screenshot(path=str(shots / f"group-{width}.png"))
            page.locator(".logs-sidebar").screenshot(path=str(shots / f"sidebar-{width}.png"))
            page.close()
        browser.close()
    assert len(jobs.provider.calls) == 3  # Only FakeAI: intro, discovery, one restaurant.
    assert len(queries.rows("jobs")) == 3
