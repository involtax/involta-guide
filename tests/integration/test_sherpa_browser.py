import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import text

from involta_guide.infrastructure.voice.consumer import Consumer
from involta_guide.main import create_app
from involta_voice.contracts import LocationContext
from involta_voice.service import VoiceService
from involta_voice.worker import Worker

from .test_jobs import setup
from .test_sherpa_comparison import GigaResults
from .test_voice_guide import client_for
from .test_voice_service import metadata
from .test_voice_service import voice_database as voice_database
from .test_web import login


def test_single_profile_and_exact_dictionary_in_existing_admin_shell(
    database, voice_database, streaming_webm
):
    settings, engine, _, _, city, _, _ = setup(database)
    vs, ve = voice_database
    vs = vs.model_copy(update={"asr_beta_enabled": True})
    settings = settings.model_copy(update={"voice_service_secret": vs.service_secret})
    with engine.begin() as conn:
        for name in ("Алхимист", "Огонёк", "Сыроварня"):
            conn.execute(
                text(
                    "INSERT INTO restaurants(id,location_id,name,cuisine,description,maps_url,active) VALUES(:id,:city,:name,'','','',0)"
                ),
                {"id": uuid4(), "city": city["id"], "name": name},
            )
    meta = metadata().model_copy(
        update={
            "location": LocationContext(
                id=city["id"],
                slug=city["slug"],
                name=city["name"],
                content_epoch=1,
                recognition_dictionary=["Алхимист", "Иваново", "Огонёк", "Сыроварня"],
            )
        }
    )
    VoiceService(ve, vs).submit(meta, streaming_webm.read_bytes(), "audio/webm")

    class Demo(GigaResults):
        def call(self, kind, request, audio, mime):
            body = super().call(kind, request, audio, mime)
            proper = request.get("variant", "").startswith("sherpa_hotwords")
            body["body"] = json.dumps(
                {
                    "text": "в иванове, плюс, из иванова. ресторан алхимист."
                    if proper
                    else "иваново, плюс, иваново.",
                    "is_fake": True,
                }
            )
            return body

    worker = Worker(ve, vs, Demo())
    assert all(worker.once() for _ in range(1))
    Consumer(engine, client_for(settings, vs)).once()
    app = create_app(settings)
    app.state.voice_client = client_for(settings, vs)
    folder = Path("var/screenshots/voice-reprocess")
    folder.mkdir(parents=True, exist_ok=True)
    with TestClient(app) as client, sync_playwright() as p:
        login(client)
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or "/usr/bin/chromium",
            args=["--no-sandbox", "--autoplay-policy=no-user-gesture-required"],
        )
        for width in (390, 1440):
            page = browser.new_page(viewport={"width": width, "height": 1100})

            def respond(route):
                parsed = urlsplit(route.request.url)
                response = client.request(
                    route.request.method,
                    parsed.path + ("?" + parsed.query if parsed.query else ""),
                    content=route.request.post_data_buffer,
                    headers={
                        **{
                            k: v
                            for k, v in route.request.headers.items()
                            if k in ("range", "if-range", "content-type")
                        },
                        **(
                            {"origin": "http://testserver"}
                            if route.request.method == "POST"
                            else {}
                        ),
                    },
                )
                route.fulfill(
                    status=response.status_code,
                    body=response.content,
                    headers=dict(response.headers),
                )

            page.route("**/*", respond)
            page.goto("http://localhost/guide/admin/reviews/ivanovo")
            expect(page.locator(".review-transcripts > div")).to_have_count(1)
            expect(page.locator(".review-transcripts")).not_to_contain_text("Parakeet")
            expect(page.locator(".recognition-before")).to_contain_text(
                "в иванове, плюс, из иванова"
            )
            expect(page.locator(".recognition-after")).to_contain_text(
                "в Иванове, плюс, из Иванова"
            )
            expect(page.locator(".recognition-after")).to_contain_text("Алхимист")
            expect(page.get_by_text("Sherpa · сильные подсказки", exact=True)).to_have_count(1)
            expect(
                page.get_by_text(
                    "Словарь: Алхимист · Иваново · Огонёк · Сыроварня · алхимист · иваново · огонёк · сыроварня",
                    exact=True,
                )
            ).to_have_count(1)
            expect(page.get_by_role("button", name="Удалить отзыв", exact=False)).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert page.locator('a[href*="/logs"]').count() == 0
            audio = page.locator("audio")
            expect(audio).to_have_attribute(
                "controlslist", "nodownload noplaybackrate noremoteplayback"
            )
            page.wait_for_function(
                "(a)=>Number.isFinite(a.duration)&&a.duration>6", arg=audio.element_handle()
            )
            width_before = audio.evaluate("a=>a.getBoundingClientRect().width")
            audio.evaluate("a=>a.play()")
            page.wait_for_timeout(400)
            assert audio.evaluate("a=>a.currentTime") > 0.1
            audio.evaluate("a=>{a.currentTime=3}")
            page.wait_for_function(
                "(a)=>a.currentTime>=3&&a.currentTime<4", arg=audio.element_handle()
            )
            assert audio.evaluate("a=>a.getBoundingClientRect().width") == width_before
            page.locator(".review-card").screenshot(
                path=str(folder / f"player-playing-{width}.png")
            )
            audio.evaluate("a=>a.pause()")

            page.screenshot(path=str(folder / f"admin-{width}.png"), full_page=True)
            page.locator(".review-card").screenshot(path=str(folder / f"card-{width}.png"))
            button = page.get_by_role("button", name="Перераспознать отзыв", exact=False)
            button.click()
            expect(button).to_be_disabled()
            expect(page.get_by_text("Перераспознаётся.", exact=False)).to_be_visible()
            page.locator(".review-card").screenshot(path=str(folder / f"pending-{width}.png"))
            assert worker.once()
            assert Consumer(engine, client_for(settings, vs)).once() == 2
            expect(button).to_be_enabled(timeout=10000)
            expect(page.get_by_text("Перераспознаётся.", exact=False)).to_have_count(0)
            expect(page.locator(".recognition-after")).to_contain_text("Алхимист")
            page.close()
        browser.close()
