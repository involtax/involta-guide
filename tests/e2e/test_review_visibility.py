import os
from pathlib import Path

from integration.test_reviews2 import SOURCE, prepared
from integration.test_voice_upload_transport import serving
from playwright.sync_api import expect, sync_playwright

from involta_guide.main import create_app


def test_author_sees_transcript_before_other_browser_and_home(database):
    settings, _, city, _, event, _, worker, provider, _ = prepared(database)
    screenshots = Path("var/screenshots/review-classification")
    screenshots.mkdir(parents=True, exist_ok=True)
    try:
        with serving(create_app(settings)) as origin, sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
            )
            owner = browser.new_context(viewport={"width": 390, "height": 844})
            owner.add_cookies(
                [{"name": "guide_visitor", "value": event["metadata"]["visitor"], "url": origin}]
            )
            other = browser.new_context(viewport={"width": 1440, "height": 900})
            author_page, other_page = owner.new_page(), other.new_page()
            path = origin + "/guide/" + city["slug"]
            author_page.goto(path)
            other_page.goto(path)
            expect(author_page.get_by_text(SOURCE, exact=True)).to_have_count(1)
            expect(other_page.get_by_text(SOURCE, exact=True)).to_have_count(0)
            author_page.get_by_text(SOURCE, exact=True).scroll_into_view_if_needed()
            author_page.screenshot(path=str(screenshots / "author-pending-mobile.png"))
            other_page.goto(origin + "/guide/")
            expect(other_page.get_by_text(SOURCE, exact=True)).to_have_count(0)
            assert worker.once()  # Mocked classifier; publication requires no manual OK.
            other_page.reload()
            expect(other_page.get_by_text(SOURCE, exact=True)).to_have_count(1)
            other_page.get_by_text(SOURCE, exact=True).scroll_into_view_if_needed()
            other_page.screenshot(path=str(screenshots / "classified-home-desktop.png"))
            other_page.goto(path)
            expect(other_page.get_by_text(SOURCE, exact=True)).to_have_count(1)
            browser.close()
    finally:
        provider.close()
