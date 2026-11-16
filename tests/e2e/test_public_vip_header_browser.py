import os
from pathlib import Path
from uuid import uuid4

from integration.test_voice_upload_transport import serving
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import text

from involta_guide.main import create_app


def test_gold_header_mobile_first_and_admin_link_do_not_overlap(database):
    settings, engine = database
    visitor = str(uuid4())
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO vip_visitors(visitor_id,level) VALUES(:id,1)"), {"id": visitor}
        )
    app = create_app(settings)
    admin = app.state.context.security.login("test-password-123456", "gold-header-browser")
    shots = Path(os.getenv("GOLD_HEADER_SCREENSHOTS_DIR", "var/screenshots/gold-header"))
    shots.mkdir(parents=True, exist_ok=True)
    with serving(app) as origin, sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        for width in (320, 390, 1280):
            ctx = browser.new_context(
                viewport={"width": width, "height": 844},
                is_mobile=width < 500,
                has_touch=width < 500,
            )
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(origin + "/guide/ivanovo")
            expect(page.locator(".brand sup")).to_have_text("Россия")
            before_height = page.locator(".header").bounding_box()["height"]
            ctx.add_cookies([{"name": "guide_visitor", "value": visitor, "url": origin}])
            page.reload()
            expect(page.locator(".brand-gold")).to_have_text("GOLD")
            expect(page.locator(".brand-crown")).to_be_visible()
            assert page.locator(".brand-crown").get_attribute("aria-hidden") == "true"
            assert page.locator(".header").bounding_box()["height"] == before_height
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            page.locator(".header").screenshot(path=str(shots / f"gold-{width}.png"))
            page.get_by_role("link", name="О GOLD").click()
            expect(page).to_have_url(origin + "/guide/gold")
            expect(page.get_by_role("heading", name="Свои люди.")).to_be_visible()
            brand = page.locator(".brand-name")
            brand.tap() if width < 500 else brand.click()
            expect(page).to_have_url(origin + "/guide/")
            ctx.add_cookies([{"name": "guide_admin", "value": admin, "url": origin}])
            page.reload()
            link = page.get_by_role("link", name="Админка", exact=True)
            expect(link).to_be_visible()
            crown = page.locator(".brand-crown").bounding_box()
            target = link.bounding_box()
            assert crown["x"] + crown["width"] < target["x"]
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            page.locator(".header").screenshot(path=str(shots / f"gold-admin-{width}.png"))
            assert not errors, errors
            ctx.close()
        browser.close()
