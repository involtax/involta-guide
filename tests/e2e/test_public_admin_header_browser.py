import os
from pathlib import Path

from integration.test_voice_upload_transport import serving
from playwright.sync_api import expect, sync_playwright

from involta_guide.main import create_app


def test_admin_link_at_small_and_large_widths_and_navigation(database):
    settings, _ = database
    app = create_app(settings)
    token = app.state.context.security.login("test-password-123456", "header-browser")
    shots = Path("docs/previews/admin-header-link")
    shots.mkdir(parents=True, exist_ok=True)
    with serving(app) as origin, sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.goto(origin + "/guide/")
        expect(page.locator(".header-admin-link")).to_have_count(0)
        page.locator("header").screenshot(path=str(shots / "guest-1440.png"))
        context.add_cookies([{"name": "guide_admin", "value": token, "url": origin}])
        for width in (1440, 390, 320):
            page.set_viewport_size({"width": width, "height": 900})
            page.goto(origin + "/guide/")
            link = page.get_by_role("link", name="Админка", exact=True)
            expect(link).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            brand = page.locator(".brand").bounding_box()
            target = link.bounding_box()
            assert brand["x"] + brand["width"] <= target["x"]
            page.locator("header").screenshot(path=str(shots / f"admin-{width}.png"))
            page.goto(origin + "/guide/about")
            expect(link).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        link.click()
        expect(page).to_have_url(origin + "/guide/admin")
        expect(page.get_by_role("button", name="Выйти", exact=True)).to_be_visible()
        expect(page.locator(".header-admin-link")).to_have_count(0)
        browser.close()
