import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

from involta_guide.application.services.locations import Queries
from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow


def test_browser_public_htmx_intro_logs_and_rebuild(database, tmp_path):
    settings, engine = database
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    base = origin + "/guide"
    env = {
        **os.environ,
        "DATABASE_URL": settings.database_url.get_secret_value(),
        "DATABASE_SCHEMA": settings.database_schema,
        "SESSION_SECRET": settings.session_secret.get_secret_value(),
        "ADMIN_PASSWORD": "test-password-123456",
        "OPENAI_API_KEY": "",
        "SITE_URL": base,
        "DATA_DIR": str(tmp_path),
        "COOKIE_SECURE": "false",
    }
    with (tmp_path / "server.log").open("w+") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "involta_guide.main:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-access-log",
            ],
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            with httpx.Client(trust_env=False) as client:
                for _ in range(80):
                    try:
                        if client.get(base + "/health/ready").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.2)
                else:
                    log.seek(0)
                    raise AssertionError(log.read())
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    executable_path=os.getenv("CHROMIUM_PATH")
                    or (
                        "/usr/bin/chromium"
                        if Path("/usr/bin/chromium").exists()
                        else p.chromium.executable_path
                    ),
                    args=[
                        "--no-sandbox",
                        "--use-fake-ui-for-media-stream",
                        "--use-fake-device-for-media-stream",
                    ],
                )
                context = browser.new_context(permissions=["microphone"])
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(base + "/ivanovo")
                expect(page.get_by_role("heading", level=1)).to_contain_text("Где поесть")
                expect(page.get_by_role("heading", name="Подборка в работе")).to_have_count(0)
                assert "noindex" in page.locator("meta[name=robots]").get_attribute("content")
                assert "/_next/" not in page.content()
                # Keep exercising the real WASM/model, without a diagnostic UI.
                bundle = (
                    Path(__file__).parents[2] / "src/involta_voice/static/vad/bundle.min.js"
                ).read_text()
                probe = """;const create=window.vad.MicVAD.new.bind(window.vad.MicVAD);
                  window.vad.MicVAD.new=options=>{const frame=options.onFrameProcessed;
                    return create({...options,onFrameProcessed:(...args)=>{window.__vadFrame=true;frame(...args)}})};"""
                page.route(
                    "**/voice/vad/bundle.min.js",
                    lambda route: route.fulfill(
                        content_type="application/javascript", body=bundle + probe
                    ),
                )
                widget = page.locator("#guide-voice")
                widget.get_by_role("button", name="Записать отзыв", exact=True).click()
                expect(widget).to_have_attribute("data-voice-state", "recording")
                page.wait_for_function("window.__vadFrame===true", timeout=15000)
                page.keyboard.press("Escape")
                expect(widget).to_have_attribute("data-voice-state", "idle")
                # The temporary public button needs no login and shows async progress/errors.
                fill = page.locator("[data-public-fill]")
                fill_button = fill.get_by_role("button", name="Запарсить эти данные", exact=True)
                expect(fill_button).to_be_visible()
                expect(page.get_by_role("link", name="Открыть логи наполнения ↗")).to_have_count(0)
                logs = context.new_page()
                logs.goto(base + "/logs")
                expect(logs.get_by_role("heading", name="Логи", exact=True)).to_be_visible()
                logs.close()
                submissions = []

                def accept_fill(route):
                    submissions.append(route.request.method)
                    route.fulfill(
                        status=202,
                        json={"state": "queued", "can_start": False, "message": "Запрос принят"},
                    )

                page.route("**/api/locations/ivanovo/fill", accept_fill)
                page.route(
                    "**/api/locations/ivanovo/fill/status",
                    lambda route: route.fulfill(
                        status=200,
                        json={
                            "state": "failed",
                            "can_start": False,
                            "message": "Тестовая ошибка поиска",
                        },
                    ),
                )
                fill_button.click()
                expect(fill_button).to_be_hidden()
                expect(fill.locator("[data-fill-waiting]")).to_be_visible()
                expect(fill.get_by_role("status")).to_have_text(
                    "Тестовая ошибка поиска", timeout=10000
                )
                expect(fill_button).to_be_hidden()
                expect(fill.locator("[data-fill-waiting]")).to_be_hidden()
                assert submissions == ["POST"]
                page.unroute("**/api/locations/ivanovo/fill")
                page.unroute("**/api/locations/ivanovo/fill/status")
                page.goto(base + "/admin")
                page.get_by_label("Пароль редакции").fill("test-password-123456")
                page.get_by_role("button", name="Войти ↗").click()
                expect(page.get_by_role("navigation", name="Администрирование")).to_be_visible()
                queries = Queries(lambda: PostgresUow(engine))
                location = queries.directory(query="Иваново")["rows"][0]
                page.goto(base + "/admin/settings/filling")
                model_form = page.locator('form[action$="/settings/filling"]')
                model_form.get_by_label("Модель новых запросов наполнения").select_option(
                    "gpt-6.1-sol"
                )
                expect(page.get_by_role("region", name="Модели наполнения")).to_contain_text(
                    "GPT-6.1 Sol"
                )
                model_form.get_by_role("button", name="Сохранить настройки наполнения").click()
                expect(page.locator(".notice[role=status]")).to_contain_text("Настройки сохранены")
                assert queries.setting("generation_model") == "gpt-6.1-sol"
                page.goto(base + "/admin/locations/" + location["slug"])
                # Current content is applied by generation/review services, not direct snapshot forms.
                from integration.editor_fixture import FixtureEditor
                from integration.test_editorial import CONTENT

                FixtureEditor(lambda: PostgresUow(engine)).save(location["id"], CONTENT, None)
                page.reload()
                expect(page.get_by_role("button", name="Наполнить через GPT ↗")).to_have_count(0)
                expect(page.get_by_role("button", name="Сохранить вступление")).to_be_visible()
                intro_form = page.locator('form[action$="/intro"]')
                intro_form.get_by_role("textbox", name="Вступление", exact=True).fill(
                    "Ручное вступление из браузера"
                )
                intro_form.get_by_role("button", name="Сохранить вступление").click()
                expect(page.locator(".notice[role=status]")).to_contain_text("Вступление сохранено")
                page.reload()
                expect(page.get_by_role("textbox", name="Вступление", exact=True)).to_have_value(
                    "Ручное вступление из браузера"
                )
                page.screenshot(path=tmp_path / "admin-location-desktop.png", full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                page.screenshot(path=tmp_path / "admin-location-mobile.png", full_page=True)
                page.set_viewport_size({"width": 1365, "height": 900})
                page.get_by_role("link", name="Настройки наполнения", exact=True).click()
                expect(page).to_have_url(base + "/admin/settings/filling")
                page.go_back()
                expect(page.get_by_role("textbox", name="Вступление", exact=True)).to_have_value(
                    "Ручное вступление из браузера"
                )
                page.go_forward()
                expect(
                    page.get_by_role("heading", name="Настройки наполнения", exact=True)
                ).to_be_visible()

                page.goto(base + "/ivanovo")
                expect(
                    page.get_by_role(
                        "heading", name="Тестовый ресторан Тестовый ресторан — Яндекс Карты"
                    )
                ).to_be_visible()
                assert "tel:+74951234567" in page.content()
                expect(
                    page.get_by_role("button", name="Запарсить эти данные", exact=True)
                ).to_have_count(0)
                expect(page.locator("[data-consent]")).to_have_count(0)
                # Real Guide carrier, real microphone/MediaRecorder, no upload or AI calls.
                screenshots = Path("var/screenshots")
                screenshots.mkdir(parents=True, exist_ok=True)
                voice = page.locator("#guide-voice")
                for width, name in [(1440, "desktop"), (390, "mobile")]:
                    page.set_viewport_size({"width": width, "height": 1000 if width > 500 else 844})
                    voice.scroll_into_view_if_needed()
                    voice.locator(".primary").evaluate("e=>e.blur()")
                    expect(
                        voice.get_by_role(
                            "heading", name="Скажи, что ты думаешь и о каком ресторане."
                        )
                    ).to_be_visible()
                    before = voice.locator(".primary").bounding_box()
                    assert (
                        abs(
                            before["x"]
                            + 36
                            - (voice.bounding_box()["x"] + voice.bounding_box()["width"] / 2)
                        )
                        <= 1
                    )
                    page.screenshot(path=screenshots / f"voice-guide-idle-{name}.png")
                    voice.get_by_role("button", name="Записать отзыв", exact=True).click()
                    expect(voice).to_have_attribute("data-voice-state", "recording")
                    during = voice.locator(".primary").bounding_box()
                    assert abs(before["y"] - during["y"]) <= 2
                    expect(voice.locator(".timer")).to_contain_text("00:")
                    page.screenshot(path=screenshots / f"voice-guide-recording-{name}.png")
                    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
                    page.keyboard.press("Escape")
                    expect(voice).to_have_attribute("data-voice-state", "idle")
                page.goto(base + "/admin/locations/ivanovo")
                rebuild = page.locator('form[action$="/rebuild"]')
                button = rebuild.get_by_role("button", name="Пересобрать подборку")
                expect(button).to_be_visible()
                before_location = queries.get("locations", location["id"])
                before_restaurants = queries.rows("restaurants", {"location_id": location["id"]})
                before_reviews = queries.rows("reviews", {"location_id": location["id"]})
                button.click()
                expect(page).to_have_url(re.compile(r"/guide/logs/ivanovo/\d+$"))
                after = queries.get("locations", location["id"])
                assert not after["reset_pending"]
                assert after["content_epoch"] == before_location["content_epoch"]
                assert after["intro"] == before_location["intro"]
                assert (
                    queries.rows("restaurants", {"location_id": location["id"]})
                    == before_restaurants
                )
                assert queries.rows("reviews", {"location_id": location["id"]}) == before_reviews
                pending = queries.rows(
                    "jobs", {"location_id": location["id"], "kind": "generate", "status": "queued"}
                )
                assert len(pending) == 1
                page.goto(base + "/admin/locations/ivanovo")
                expect(page.locator('form[action$="/rebuild"] button')).to_be_disabled()
                assert not errors
                browser.close()
        finally:
            process.terminate()
            process.wait(timeout=15)
