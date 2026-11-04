import os
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

from involta_guide.main import create_app

from .test_web import login
from .voice_fixture import imported


def test_one_server_rendered_feed_and_admin_delete_at_mobile_and_desktop(database):
    settings, _, _, _, city, _, _ = imported(database)
    app = create_app(settings)
    screenshots = Path("var/screenshots")
    screenshots.mkdir(parents=True, exist_ok=True)
    with TestClient(app) as client, sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or "/usr/bin/chromium", args=["--no-sandbox"]
        )
        for width in (390, 1440):
            client.cookies.clear()
            page = browser.new_page(viewport={"width": width, "height": 1000})

            def respond(route):
                url = urlsplit(route.request.url)
                if url.path.endswith("widget.v1.js"):
                    route.fulfill(
                        content_type="application/javascript",
                        body=Path("src/involta_voice/static/widget.v1.js").read_text(),
                    )
                else:
                    response = client.get(url.path + ("?" + url.query if url.query else ""))
                    route.fulfill(
                        status=response.status_code,
                        body=response.content,
                        content_type=response.headers.get("content-type", "text/plain"),
                    )

            page.route("**/*", respond)
            page.goto("http://localhost/guide/" + city["slug"])
            expect(page.locator("[data-public-reviews] .reviews > p")).to_have_count(1)
            expect(page.locator("#guide-voice .reviews")).to_have_count(0)
            expect(page.locator("#guide-voice .primary")).to_be_visible()
            expect(page.get_by_role("heading", name="Отзывы", exact=True)).to_have_count(1)
            page.locator("#guide-voice").scroll_into_view_if_needed()
            page.screenshot(path=str(screenshots / f"public-reviews-{width}.png"), full_page=True)
            login(client)
            page.goto("http://localhost/guide/admin/reviews/" + city["slug"])
            expect(page.get_by_role("button", name="Удалить отзыв", exact=False)).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(screenshots / f"admin-review-delete-{width}.png"))
            page.close()
        browser.close()
