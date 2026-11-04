import os

from integration.test_voice_upload_transport import serving
from playwright.sync_api import expect, sync_playwright

from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow
from involta_guide.infrastructure.security import Security
from involta_guide.main import create_app
from involta_stats import daily_stats


def test_about_cookie_and_statistics_table_at_desktop_and_mobile_widths(database, tmp_path):
    settings, engine = database
    app = create_app(settings)
    token = Security(settings, lambda: PostgresUow(engine)).login("test-password-123456", "test-ip")
    with serving(app) as origin, sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        context = browser.new_context(viewport={"width": 390, "height": 950})
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(origin + "/guide/")
        footer = page.locator("footer")
        expect(footer.get_by_role("link", name="Я тут в первый раз")).to_be_visible()
        assert footer.get_by_role("link").count() == 1
        footer.get_by_role("link").click()
        expect(page.get_by_role("heading", name="О проекте")).to_be_visible()
        expect(footer.get_by_role("link", name="О проекте")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(tmp_path / "about-mobile.png"), full_page=True)
        page.get_by_role("link", name="Выбрать город").click()
        expect(footer.get_by_role("link", name="О проекте")).to_be_visible()
        assert daily_stats(engine)[0].hits == 3
        assert daily_stats(engine)[0].uniques == 1
        context.add_cookies([{"name": "guide_admin", "value": token, "url": origin}])
        for width in (1440, 390):
            page.set_viewport_size({"width": width, "height": 950})
            page.goto(origin + "/guide/admin/stats")
            expect(page.get_by_role("heading", name="Статистика")).to_be_visible()
            expect(page.get_by_role("link", name="Статистика", exact=True)).to_have_attribute(
                "aria-current", "page"
            )
            table = page.locator(".stats-table")
            assert table.locator("thead time").count() == 30
            assert table.locator("tbody tr").count() == 4
            expect(table.locator("tbody tr").last.locator("td").first).to_have_text("3,0")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            if width == 390:
                label = table.get_by_role("rowheader", name="Хитов на уника")
                left = label.bounding_box()["x"]
                page.locator(".stats-scroll").evaluate("el => el.scrollLeft = el.scrollWidth")
                assert abs(label.bounding_box()["x"] - left) < 1
                page.locator(".stats-scroll").evaluate("el => el.scrollLeft = 0")
            page.screenshot(path=str(tmp_path / f"stats-{width}.png"), full_page=True)
        assert daily_stats(engine)[0].hits == 3  # All administrative navigation was excluded.
        assert errors == []
        browser.close()
