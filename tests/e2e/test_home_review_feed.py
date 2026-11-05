import os
from datetime import UTC, datetime, timedelta

from integration.test_home_reviews import add_review
from integration.test_voice_upload_transport import serving
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import text

from involta_guide.main import create_app


def test_approved_home_feed_layout_and_city_navigation(database, tmp_path):
    settings, engine = database
    with engine.connect() as connection:
        cities = {
            row["slug"]: row["id"]
            for row in connection.execute(
                text(
                    "SELECT id,slug FROM locations WHERE slug IN ('ivanovo','veliky-novgorod','ples')"
                )
            ).mappings()
        }
    assert len(cities) == 3
    slugs = list(cities)
    moment = datetime(2026, 11, 6, 11, 32, tzinfo=UTC)
    for i in range(10):
        add_review(
            engine,
            cities[slugs[i % 3]],
            f"Отзыв {i + 1}. Паста очень хорошая, соус не тяжёлый. Принесли быстро, хотя почти все столы были заняты. Вернусь попробовать остальное.",
            moment - timedelta(hours=i),
        )
    with serving(create_app(settings)) as origin, sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        for label, width, height in [
            ("desktop", 1440, 1100),
            ("mobile", 390, 950),
            ("small-mobile", 320, 850),
        ]:
            page = browser.new_page(
                viewport={"width": width, "height": height}, has_touch=width < 650
            )
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(origin + "/guide/", wait_until="networkidle")
            expect(page.get_by_role("heading", name="Жизнь кипит", exact=True)).to_be_visible()
            expect(page.locator(".home-review")).to_have_count(10)
            expect(page.locator(".search-intro")).to_have_count(0)
            expect(page.get_by_text("Куда отправимся?", exact=True)).to_have_count(0)
            assert (
                page.locator(".search-hero > .eyebrow").evaluate(
                    "el=>getComputedStyle(el).textTransform"
                )
                == "none"
            )
            assert (
                page.locator(".home-review").last.evaluate(
                    "el=>getComputedStyle(el).borderBottomWidth"
                )
                == "0px"
            )
            assert (
                page.locator("footer").evaluate("el=>getComputedStyle(el).borderTopWidth") == "0px"
            )
            for review in page.locator(".home-review").all():
                box = review.bounding_box()
                city = review.locator(".home-review-city").bounding_box()
                stamp = review.locator("time").bounding_box()
                assert city["x"] + city["width"] <= stamp["x"] - 8
                assert abs(stamp["x"] + stamp["width"] - box["x"] - box["width"]) < 2
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            page.screenshot(path=str(tmp_path / f"home-{label}.png"), full_page=True)
            if label == "mobile":
                page.screenshot(path=str(tmp_path / "home-mobile-first-screen.png"))
            first = page.locator(".home-review").first
            href = first.get_attribute("href")
            if width < 650:
                first.tap()
            else:
                first.focus()
                first.press("Enter")
            page.wait_for_url(origin + href)
            expect(page.locator("h1")).to_be_visible()
            assert not errors, errors
            page.close()
        browser.close()
