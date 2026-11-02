import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

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
                expect(
                    logs.get_by_role("heading", name="Публичные логи наполнения")
                ).to_be_visible()
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
                page.goto(base + "/admin?tab=locations&location=" + location["id"])
                page.get_by_role("button", name="+ Ресторан", exact=True).click()
                card = page.locator("[data-card]")
                expect(card).to_have_count(1)
                card.get_by_label("Название", exact=True).fill("Тестовый ресторан")
                card.get_by_label("Кухня", exact=True).fill("Русская")
                card.get_by_label("Почему стоит зайти").fill("Карточка для проверки публикации")
                card.get_by_label("Телефон (обязателен для публикации)").fill("+74951234567")
                card.get_by_label("Яндекс Карты").fill("https://yandex.ru/maps/org/test/123/")
                page.get_by_label("Вступление").fill("Тестовая подборка")
                page.get_by_role("button", name="Сохранить новую версию", exact=True).click()
                expect(page.get_by_role("status")).to_contain_text("Черновик сохранён")
                page.get_by_role("button", name="Опубликовать текущий черновик ↗").click()
                expect(page.get_by_role("status")).to_contain_text("Версия опубликована")
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
                page.get_by_role("checkbox").check()
                page.get_by_role("button", name="● Записать отзыв").click()
                time.sleep(2.2)
                page.get_by_role("button", name="Остановить", exact=False).click()
                page.get_by_role("button", name="Отправить ↗").click()
                expect(page.get_by_text("Получен · в очереди", exact=True)).to_be_visible(
                    timeout=10000
                )
                review = queries.rows("reviews")[0]
                page.goto(base + "/admin?tab=reviews&review=" + review["id"])
                page.get_by_role("button", name="Одобрить отзыв", exact=True).click()
                expect(page.get_by_role("status")).to_contain_text("Решение сохранено")
                page.get_by_role("link", name="Наполнение", exact=True).click()
                expect(page.get_by_role("heading", name="Наполнение", exact=True)).to_be_visible()
                assert page.locator("#admin-main").count() == 1
                job = queries.rows("jobs", {"review_id": review["id"]})[0]
                AIAudit(engine, settings).record(
                    job["id"],
                    "job_failed",
                    {
                        "validation_errors": [
                            {"loc": ["restaurants", 0, "phone"], "msg": "Телефон неверен"}
                        ],
                        "traceback": "browser audit fixture",
                    },
                )
                page.get_by_role("link", name="Аудит ИИ", exact=True).click()
                expect(page.get_by_role("heading", name="Аудит ИИ", exact=True)).to_be_visible()
                page.goto(base + "/admin?tab=ai-audit&job=" + job["id"])
                expect(page.locator("pre").filter(has_text="Телефон неверен")).to_be_visible()
                with page.expect_download() as downloaded:
                    page.get_by_role("link", name="Скачать полный аудит JSON ↗").click()
                export = json.loads(Path(downloaded.value.path()).read_text())
                assert export["job"]["id"] == job["id"]
                # Alex bootstrap is not a named parameter and is cleared from the URL.
                alex = browser.new_context()
                alex_page = alex.new_page()
                alex_page.goto(base + "/?" + settings.alex_secret.get_secret_value())
                expect(alex_page).to_have_url(base + "/")
                alex_page.goto(base + "/ivanovo")
                expect(alex_page.get_by_text("Режим Алекса:", exact=False)).to_be_visible()
                page.goto(base + "/alex")
                page.get_by_role("button", name="Завершить все сессии Алекса").click()
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
