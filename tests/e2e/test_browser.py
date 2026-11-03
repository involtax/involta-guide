import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
from playwright.sync_api import expect, sync_playwright

from involta_guide.application.services.locations import Queries
from involta_guide.infrastructure.ai.audit import AIAudit
from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow


def test_browser_public_htmx_microphone_and_alex(database, tmp_path):
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
        "ALEX_SECRET": settings.alex_secret.get_secret_value(),
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
                expect(page.get_by_role("heading", name="Подборка в работе")).to_be_visible()
                assert "noindex" in page.locator("meta[name=robots]").get_attribute("content")
                assert "/_next/" not in page.content()
                # The temporary public button needs no login and shows async progress/errors.
                fill = page.locator("[data-public-fill]")
                fill_button = fill.get_by_role("button", name="Запарсить эти данные", exact=True)
                expect(fill_button).to_be_visible()
                expect(page.get_by_role("link", name="Открыть логи наполнения ↗")).to_be_visible()
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
                            "can_start": True,
                            "message": "Тестовая ошибка поиска",
                        },
                    ),
                )
                fill_button.click()
                expect(fill_button).to_be_disabled()
                expect(fill.get_by_role("status")).to_have_text(
                    "Тестовая ошибка поиска", timeout=10000
                )
                expect(fill_button).to_be_enabled()
                assert submissions == ["POST"]
                page.unroute("**/api/locations/ivanovo/fill")
                page.unroute("**/api/locations/ivanovo/fill/status")
                page.goto(base + "/admin")
                page.get_by_label("Пароль редакции").fill("test-password-123456")
                page.get_by_role("button", name="Войти ↗").click()
                expect(page.get_by_role("heading", name="Рабочий стол.")).to_be_visible()
                queries = Queries(lambda: PostgresUow(engine))
                location = queries.directory(query="Иваново")["rows"][0]
                page.goto(base + "/admin/settings/filling")
                model_form = page.locator('form[action$="/settings/filling"]')
                model_form.get_by_label("Модель новых запросов наполнения").select_option(
                    "gpt-6.1-sol"
                )
                expect(page.get_by_role("region", name="Сравнение моделей и цен")).to_contain_text(
                    "$0.12"
                )
                model_form.get_by_role("button", name="Сохранить настройки наполнения").click()
                expect(page.get_by_role("status")).to_contain_text("Настройки сохранены")
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
                expect(page.get_by_role("status")).to_contain_text("Вступление сохранено")
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
                # One widget uses the server session to mark administrator commands.
                page.goto(base + "/ivanovo")
                page.get_by_role("button", name="Записать отзыв", exact=True).click()
                # Permission/device startup is not recorded audio. Wait for the actual
                # recorder timer rather than sleeping from the initial button press.
                expect(page.locator("#guide-voice").locator(".stage .timer")).to_have_text(
                    "00:05", timeout=15000
                )
                page.get_by_role("button", name="Остановить", exact=False).click()
                page.get_by_role("button", name="Отправить отзыв").click()
                expect(page.get_by_role("status")).to_contain_text(
                    "Запись доставлена", timeout=10000
                )
                review = queries.rows("reviews")[0]
                page.goto(base + "/admin?tab=reviews&review=" + review["id"])
                expect(page).to_have_url(base + "/admin/reviews/ivanovo/" + str(review["number"]))
                expect(
                    page.get_by_text(
                        "Команда выполняется существующим обработчиком Алекса;", exact=False
                    )
                ).to_be_visible()
                expect(page.get_by_role("region", name="Сравнение распознавания")).to_be_visible()
                expect(
                    page.get_by_role("button", name="Одобрить весь пакет", exact=True)
                ).to_have_count(0)
                page.get_by_role("link", name="Настройки наполнения", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Настройки наполнения", exact=True)
                ).to_be_visible()
                assert page.locator("#admin-main").count() == 1
                job = queries.rows("jobs", {"review_id": review["id"]})[0]
                AIAudit(engine, settings).record(
                    job["id"],
                    "job_failed",
                    {
                        "validation_errors": [
                            {"loc": ["restaurants", 0, "phone"], "msg": "Телефон неверен"}
                        ],
                        "traceback": "browser audit fixture\n" * 200,
                    },
                )
                page.get_by_role("link", name="Логи", exact=True).click()
                expect(page.get_by_role("heading", name="Логи", exact=True)).to_be_visible()
                with PostgresUow(engine) as tx:
                    reference = tx.repo.log_reference(job["id"])
                page.goto(base + reference["path"])
                expect(page.locator("pre").filter(has_text="Телефон неверен")).to_be_visible()
                sidebar = page.get_by_role("complementary", name="Запросы").bounding_box()
                content = page.get_by_role("region", name="Выбранный лог").bounding_box()
                assert sidebar["x"] < content["x"]
                for width in (1280, 390):
                    page.set_viewport_size({"width": width, "height": 844})
                    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
                    assert (
                        page.evaluate("""() => [...document.querySelectorAll('#logs-main *')].filter(e => {
                        const s=getComputedStyle(e);
                        return e.clientHeight>0 && ((['auto','scroll'].includes(s.overflowY) && e.scrollHeight>e.clientHeight+1)
                            || (['auto','scroll'].includes(s.overflowX) && e.scrollWidth>e.clientWidth+1));
                    }).length""")
                        == 0
                    )
                page.set_viewport_size({"width": 1280, "height": 844})
                with page.expect_download() as downloaded:
                    page.get_by_role("link", name="Скачать полный JSON ↗").click()
                export = json.loads(Path(downloaded.value.path()).read_text())
                assert export["job"]["id"] == job["id"]
                failed_id = str(uuid4())
                with PostgresUow(engine) as tx:
                    tx.repo.queue(
                        {
                            "id": failed_id,
                            "kind": "generate",
                            "location_id": location["id"],
                            "prompt": "old browser fixture prompt",
                        }
                    )
                    tx.repo.change("jobs", failed_id, {"status": "failed", "error": "old failure"})
                    tx.repo.audit("temporary_public_fill", failed_id, location["id"])
                page.goto(base + "/logs/ivanovo/2")
                repeat = page.locator('form[action$="/regenerate"]')
                repeat.get_by_label("Модель нового запроса").select_option("gpt-6-astra")
                repeat.get_by_role("button", name="Повторить новым запросом").click()
                expect(page.get_by_role("heading", name="Иваново / 3", exact=False)).to_be_visible()
                expect(page).to_have_url(base + "/logs/ivanovo/3")
                new_job = queries.rows("jobs", {"kind": "generate", "status": "queued"})[0]
                assert new_job["id"] != failed_id
                config = queries.rows(
                    "audit", {"action": "generation_run", "target": new_job["id"]}
                )[0]
                assert json.loads(config["detail"])["model"] == "gpt-6-astra"
                page.goto(base + "/logs/ivanovo/3")
                expect(page.get_by_role("heading", name="Иваново / 3", exact=False)).to_be_visible()
                # Status updates without navigation or replacing the model selection form.
                with PostgresUow(engine) as tx:
                    tx.repo.change(
                        "jobs",
                        new_job["id"],
                        {"status": "failed", "error": "Проверка живого статуса"},
                    )
                expect(page.locator("#log-live [data-error]")).to_have_text(
                    "Проверка живого статуса", timeout=15000
                )
                expect(page.locator("#log-live [data-stage]")).to_have_text("Ошибка")
                expect(page.get_by_role("link", name="Скачать полный JSON ↗")).to_have_attribute(
                    "href", "/guide/logs/ivanovo/3.json?download=1"
                )
                # Alex bootstrap is not a named parameter and is cleared from the URL.
                alex = browser.new_context()
                alex_page = alex.new_page()
                alex_page.goto(base + "/?" + settings.alex_secret.get_secret_value())
                expect(alex_page).to_have_url(base + "/")
                alex_page.goto(base + "/ivanovo")
                expect(alex_page.get_by_text("Режим Алекса:", exact=False)).to_be_visible()
                page.goto(base + "/alex")
                with page.expect_response(
                    lambda r: r.request.method == "POST" and r.url.endswith("/revoke-alex")
                ):
                    page.get_by_role("button", name="Завершить все сессии Алекса").click()
                # Wait for the HTMX redirect before inspecting another session.
                expect(page.get_by_text("Для входа откройте персональную ссылку.")).to_be_visible()
                alex_page.goto(base + "/alex")
                expect(
                    alex_page.get_by_text("Для входа откройте персональную ссылку.")
                ).to_be_visible()
                alex.close()
                nojs = browser.new_context(java_script_enabled=False)
                nojs_page = nojs.new_page()
                nojs_page.goto(base + "/ivanovo")
                expect(
                    nojs_page.get_by_role("heading", name="Тестовый ресторан", exact=False)
                ).to_be_visible()
                nojs.close()
                page.set_viewport_size({"width": 390, "height": 844})
                page.goto(base + "/ivanovo")
                assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
                shots = Path("var/screenshots")
                shots.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(shots / "python-public-mobile.png"), full_page=True)
                assert not errors, errors
                browser.close()
        finally:
            process.terminate()
            process.wait(timeout=15)
