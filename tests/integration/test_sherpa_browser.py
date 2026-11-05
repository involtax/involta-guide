import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

from involta_guide.infrastructure.voice.consumer import Consumer
from involta_guide.main import create_app
from involta_voice.contracts import LocationContext
from involta_voice.service import VoiceService
from involta_voice.worker import Worker

from .test_jobs import setup
from .test_sherpa_comparison import GigaResults
from .test_voice_guide import client_for
from .test_voice_service import metadata, recording
from .test_voice_service import voice_database as voice_database
from .test_web import login


def test_single_profile_and_exact_dictionary_in_existing_admin_shell(database, voice_database):
    settings, engine, _, _, city, _, _ = setup(database)
    vs, ve = voice_database
    vs = vs.model_copy(update={"asr_beta_enabled": True})
    settings = settings.model_copy(update={"voice_service_secret": vs.service_secret})
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
    VoiceService(ve, vs).submit(meta, recording(), "audio/wav")

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
    folder = Path("var/screenshots/sherpa-comparison")
    folder.mkdir(parents=True, exist_ok=True)
    with TestClient(app) as client, sync_playwright() as p:
        login(client)
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or "/usr/bin/chromium", args=["--no-sandbox"]
        )
        for width in (390, 1440):
            page = browser.new_page(viewport={"width": width, "height": 1100})

            def respond(route):
                parsed = urlsplit(route.request.url)
                response = client.get(parsed.path + ("?" + parsed.query if parsed.query else ""))
                route.fulfill(
                    status=response.status_code,
                    body=response.content,
                    content_type=response.headers.get("content-type", "text/plain"),
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
            page.screenshot(path=str(folder / f"admin-{width}.png"), full_page=True)
            page.locator(".review-card").screenshot(path=str(folder / f"card-{width}.png"))
            page.close()
        browser.close()
