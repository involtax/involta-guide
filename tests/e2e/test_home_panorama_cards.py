"""The homepage now features panoramas; reviews stay on city pages."""

import os
from pathlib import Path

from integration.test_voice_upload_transport import serving
from playwright.sync_api import expect, sync_playwright

from involta_guide.main import create_app


def test_home_panorama_spoilers_and_navigation(database, tmp_path):
    settings, _ = database
    with serving(create_app(settings)) as origin, sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        for width in (320, 390, 768, 1440):
            page = browser.new_page(viewport={"width": width, "height": 950}, has_touch=True)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(origin + "/guide/")
            expect(page.get_by_role("heading", name="Узнаёшь город?")).to_be_visible()
            expect(page.locator(".home-city")).to_have_count(3)
            expect(page.locator(".home-reviews")).to_have_count(0)
            first = page.locator(".home-city-name").first
            first.scroll_into_view_if_needed()
            expect(first).to_have_attribute("data-concealed", "")
            before = page.url
            first.tap()
            assert page.url == before
            expect(first).not_to_have_attribute("data-concealed", "")
            expect(first).to_have_text("Иваново")
            expect(first).to_have_css("font-style", "italic")
            for image in page.locator(".home-city-image").all():
                image.scroll_into_view_if_needed()
                image.evaluate("img=>img.decode()")
                assert image.evaluate("""img => {
                    const r=img.getBoundingClientRect(), card=img.closest('article').getBoundingClientRect();
                    return img.naturalWidth===2172 && img.naturalHeight===724
                        && Math.abs(r.width/r.height-3)<.01
                        && r.left>=card.left && r.right<=card.right+.1
                        && r.top>=card.top && r.bottom<=card.bottom;
                }""")
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            folder = Path(os.getenv("HOME_SCREENSHOTS_DIR", str(tmp_path)))
            folder.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(folder / f"home-{width}.png"), full_page=True)
            first.press("Enter")
            page.wait_for_url(origin + "/guide/ivanovo")
            page.goto(origin + "/guide/")
            page.locator(".home-city-link").nth(1).click(position={"x": 30, "y": 80})
            page.wait_for_url(origin + "/guide/ples")
            assert not errors, errors
            page.close()

        page = browser.new_page(reduced_motion="reduce")
        page.goto(origin + "/guide/")
        first = page.locator(".home-city-name").first
        first.scroll_into_view_if_needed()
        canvas = first.locator("canvas")
        initial = canvas.evaluate("el=>el.toDataURL()")
        page.wait_for_timeout(150)
        assert canvas.evaluate("el=>el.toDataURL()") == initial
        first.press("Enter")
        expect(first).to_have_text("Иваново")
        page.close()
        page = browser.new_page(java_script_enabled=False)
        page.goto(origin + "/guide/")
        first = page.locator(".home-city-name").first
        expect(first).to_have_text("Иваново")
        expect(first.locator("span")).to_have_css("opacity", "1")
        first.click()
        page.wait_for_url(origin + "/guide/ivanovo")
        browser.close()
