import os
from pathlib import Path

from integration.test_upload_logs import upload
from integration.test_voice_upload_transport import serving
from playwright.sync_api import expect, sync_playwright

from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow
from involta_guide.infrastructure.security import Security
from involta_guide.main import create_app


def test_upload_logs_use_shared_admin_shell_and_mobile_layout(database, tmp_path):
    settings, engine, _, request_id = upload(database)
    app = create_app(settings)
    token = Security(settings, lambda: PostgresUow(engine)).login("test-password-123456", "test-ip")
    with serving(app) as origin, sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH")
            or ("/usr/bin/chromium" if Path("/usr/bin/chromium").exists() else None),
            args=["--no-sandbox"],
        )
        for name, width, height in (("desktop", 1440, 1100), ("mobile", 390, 844)):
            context = browser.new_context(
                viewport={"width": width, "height": height}, timezone_id="Africa/Addis_Ababa"
            )
            context.add_cookies([{"name": "guide_admin", "value": token, "url": origin}])
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(origin + "/guide/logs?kind=uploads")
            expect(page.get_by_role("heading", name="Рабочий стол.")).to_be_visible()
            expect(page.get_by_role("heading", name="Браузер получил HTTP 504")).to_be_visible()
            page.get_by_role("navigation", name="Список отправок").get_by_role("link").click()
            expect(page).to_have_url(
                origin + f"/guide/logs/ivanovo/uploads/{request_id}?kind=uploads&q=&status=&page=1"
            )
            expect(page.locator(".logs-content time[data-local-time]").first).to_contain_text(
                "GMT+3"
            )
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            assert not errors
            page.screenshot(path=str(tmp_path / f"voice-upload-log-{name}.png"), full_page=True)
            page.get_by_role("link", name="Наполнение страниц", exact=True).click()
            expect(
                page.get_by_label("Тип журналов").get_by_role("link", name="Наполнение страниц")
            ).to_have_attribute("aria-current", "page")
            context.close()
        browser.close()
