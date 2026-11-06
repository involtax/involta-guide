import os
import time

from integration.test_location_search import add_location
from integration.test_voice_upload_transport import serving
from playwright.sync_api import sync_playwright

from involta_guide.main import create_app
from involta_guide.web.http import RECENT_LOCATIONS_COOKIE


def test_browser_keeps_recent_locations_across_navigation_and_saved_session(database):
    settings, engine = database
    first = add_location(engine, "История Первый", "recent-first", ready=True)
    second = add_location(engine, "История Второй", "recent-second", ready=True)
    with serving(create_app(settings)) as origin, sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        context = browser.new_context()
        page = context.new_page()
        page.goto(origin + "/guide/recent-first")
        page.goto(origin + "/guide/recent-second")
        cookie = next(c for c in context.cookies() if c["name"] == RECENT_LOCATIONS_COOKIE)
        assert cookie["value"] == f"{second}.{first}"
        assert cookie["expires"] > time.time() + 89 * 86400
        assert cookie["httpOnly"] and cookie["sameSite"] == "Lax"
        assert RECENT_LOCATIONS_COOKIE not in page.evaluate("document.cookie")
        state = context.storage_state()
        context.close()
        restored = browser.new_context(storage_state=state)
        page = restored.new_page()
        page.goto(origin + "/guide/recent-first")
        cookie = next(c for c in restored.cookies() if c["name"] == RECENT_LOCATIONS_COOKIE)
        assert cookie["value"] == f"{first}.{second}"
        browser.close()
