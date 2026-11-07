import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from integration.test_home_reviews import add_review, prepared_pulse
from integration.test_reviews2 import state
from integration.test_voice_upload_transport import serving
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import text

from involta_guide.main import create_app


def test_approved_home_feed_layout_and_city_navigation(database, tmp_path):
    settings, engine, _, confirmed, _, service, worker, provider, _ = prepared_pulse(
        database,
        restaurant_name="Очень длинное название ресторана с летней верандой",
        author="Александр Константинопольский",
    )
    try:
        assert worker.once()
        service.approve(confirmed, str(state(engine, confirmed)["job_id"]), "admin")
    finally:
        provider.client.close()
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
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE reviews SET created_at=:moment WHERE id=:id"),
            {"id": confirmed, "moment": moment + timedelta(hours=1)},
        )
    for i in range(9):
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
            ("tablet", 768, 1000),
            ("mobile", 390, 950),
            ("small-mobile", 320, 850),
        ]:
            page = browser.new_page(
                viewport={"width": width, "height": height}, has_touch=width < 650
            )
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(origin + "/guide/", wait_until="networkidle")
            expect(page.get_by_role("heading", name="Пульс:", exact=True)).to_be_visible()
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
            first = page.locator(".home-review").first
            expect(first.locator(".home-review-restaurant")).to_have_css("font-size", "20px")
            expect(first.locator(".home-review-city")).to_have_css("font-size", "15px")
            expect(first.locator(".home-review-city")).to_have_css(
                "text-decoration-line", "underline"
            )
            metrics = first.evaluate("""el=>{
                const box=s=>el.querySelector(s).getBoundingClientRect();
                const place=box('.home-review-place'), meta=box('.home-review-meta');
                const title=box('.home-review-restaurant'), city=box('.home-review-city');
                const paragraph=el.querySelector('p'), style=getComputedStyle(paragraph);
                return {gap:meta.x-place.right, sameLine:Math.abs(title.y-city.y)<10,
                    textGap:paragraph.getBoundingClientRect().y-Math.max(meta.bottom,place.bottom),
                    ratio:parseFloat(style.lineHeight)/parseFloat(style.fontSize)};
            }""")
            assert metrics["gap"] >= 9 and metrics["sameLine"]
            assert metrics["textGap"] >= 9 and abs(metrics["ratio"] - 1.55) < 0.001
            expect(
                page.locator(".home-review").nth(1).locator(".home-review-author")
            ).to_have_count(0)
            expect(
                page.locator(".home-review").nth(1).locator(".home-review-restaurant")
            ).to_have_count(0)
            screenshot_dir = Path(os.getenv("PULSE_SCREENSHOT_DIR", str(tmp_path)))
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot_dir / f"home-{label}.png"), full_page=True)
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
