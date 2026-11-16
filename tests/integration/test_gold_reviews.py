import os
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import text

from involta_guide.infrastructure.voice.read_model import VoiceReadModel
from involta_guide.main import create_app

from .voice_fixture import classified, imported


def test_gold_is_current_author_status_not_viewer_status(database):
    settings, engine, _, _, city, reference, _ = imported(database)
    classified(engine, reference)
    read = VoiceReadModel(engine)
    with engine.begin() as conn:
        visitor = conn.execute(
            text("SELECT visitor FROM reviews WHERE id=:id"), {"id": reference}
        ).scalar_one()
    with TestClient(create_app(settings)) as client:
        assert client.get("/guide/gold").status_code == 200
        assert not read.public_texts(city["id"])[0]["author_is_gold"]
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO vip_visitors(visitor_id,level) VALUES(:id,1)"), {"id": visitor}
            )
        for rows in (read.public_texts(city["id"]), read.latest_public_texts()):
            assert rows[0]["author_is_gold"] is True
            assert "visitor" not in rows[0]
        page = client.get("/guide/" + city["slug"]).text
        assert 'class="review-crown"' in page
        assert 'class="brand-gold"' not in page
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM vip_visitors WHERE visitor_id=:id"), {"id": visitor})
        assert not read.public_texts(city["id"])[0]["author_is_gold"]
        assert 'class="review-crown"' not in client.get("/guide/" + city["slug"]).text


def test_gold_page_and_polling_preserve_crown(database):
    settings, engine, _, _, city, reference, _ = imported(database)
    classified(engine, reference)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO vip_visitors(visitor_id,level) SELECT visitor,1 FROM reviews WHERE id=:id"
            ),
            {"id": reference},
        )
        conn.execute(text("UPDATE reviews SET author_name='Алекс' WHERE id=:id"), {"id": reference})
    shots = Path(os.getenv("GOLD_SCREENSHOTS_DIR", "/workspace/artifacts/gold-preview"))
    shots.mkdir(parents=True, exist_ok=True)
    with TestClient(create_app(settings)) as client, sync_playwright() as p:
        browser = p.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        for width in (320, 390, 1440):
            page = browser.new_page(viewport={"width": width, "height": 900})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))

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
            page.goto("http://localhost/guide/gold")
            expect(page.get_by_role("heading", name="Свои люди.")).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            page.screenshot(path=str(shots / f"gold-{width}.png"), full_page=True)
            page.goto("http://localhost/guide/" + city["slug"])
            page.wait_for_function(
                "document.querySelector('[data-public-reviews] time').dateTime.endsWith('Z')"
            )
            author = page.locator("[data-public-reviews] .review-author")
            expect(author).to_have_text("Алекс")
            expect(author.locator(".review-crown")).to_be_visible()
            assert author.locator("img").evaluate("(img)=>img.complete && img.naturalWidth>0")
            page.locator("[data-public-reviews]").screenshot(
                path=str(shots / f"review-{width}.png")
            )
            with engine.begin() as conn:
                conn.execute(
                    text("UPDATE reviews SET author_name=NULL WHERE id=:id"), {"id": reference}
                )
            expect(author).to_have_text("", timeout=20000)
            expect(author.locator(".review-crown")).to_be_visible()
            with engine.begin() as conn:
                conn.execute(
                    text("UPDATE reviews SET author_name='Алекс' WHERE id=:id"), {"id": reference}
                )
            assert not errors
            page.close()
        browser.close()
