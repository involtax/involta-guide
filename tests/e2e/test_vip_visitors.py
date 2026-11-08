import os
from pathlib import Path

from fastapi.testclient import TestClient
from integration.test_vip_visitors import vip_rows, watched
from integration.test_web import login
from playwright.sync_api import expect, sync_playwright

from involta_guide.main import create_app


def test_vip_auto_grant_confirmation_and_revoke_in_admin_browser(database):
    settings, engine, _, _, _, _, worker, provider, _ = watched(database)
    shots = Path("var/screenshots/vip-visitors")
    shots.mkdir(parents=True, exist_ok=True)
    try:
        assert worker.once()
        with TestClient(create_app(settings)) as client, sync_playwright() as p:
            login(client)
            browser = p.chromium.launch(
                executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
            )
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def respond(route):
                response = client.request(
                    route.request.method,
                    route.request.url,
                    headers={
                        k: v
                        for k, v in route.request.headers.items()
                        if k
                        in ("hx-request", "hx-history-restore-request", "origin", "content-type")
                    },
                    content=route.request.post_data,
                )
                route.fulfill(
                    status=response.status_code,
                    body=response.content,
                    headers=dict(response.headers),
                )

            page.route("**/*", respond)
            for label, width in [("desktop", 1440), ("mobile", 390)]:
                page.set_viewport_size({"width": width, "height": 950})
                page.goto(
                    "http://testserver/guide/admin/reviews-2/watched", wait_until="networkidle"
                )
                expect(page.get_by_text("VIP · уровень 1", exact=False)).to_be_visible()
                assert page.locator(".reviews2-actions button").all_text_contents() == [
                    "Досмотрел",
                    "Удалить",
                ]
                assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
                page.screenshot(path=str(shots / (label + ".png")), full_page=True)
            page.get_by_role("button", name="Досмотрел", exact=True).click()
            expect(page.get_by_text("Вручную: Досмотрел", exact=True)).to_be_visible()
            assert len(vip_rows(engine)) == 1
            page.get_by_role("button", name="Удалить", exact=True).click()
            expect(page.get_by_text("Здесь пока нет отзывов.", exact=True)).to_be_visible()
            assert not vip_rows(engine)
            assert not errors
            browser.close()
    finally:
        provider.close()
