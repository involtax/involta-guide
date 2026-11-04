import json
import os
import re
from urllib.parse import parse_qs, urlsplit

from integration.editor_fixture import FixtureEditor
from integration.test_editorial import CONTENT
from integration.test_voice_upload_transport import serving
from playwright.sync_api import expect, sync_playwright

from involta_guide.application.services.locations import Queries
from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow
from involta_guide.main import create_app


def test_real_search_keyboard_touch_main_page_submit_and_javascript_free_form(database, tmp_path):
    settings, engine = database

    def factory():
        return PostgresUow(engine)

    city = Queries(factory).directory(query="Великий Новгород")["rows"][0]
    FixtureEditor(factory).save(city["id"], CONTENT, None)
    with serving(create_app(settings)) as origin, sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        for label, width, height in [("desktop", 1440, 1100), ("mobile", 390, 950)]:
            page = browser.new_page(
                viewport={"width": width, "height": height}, has_touch=width == 390
            )
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(origin + "/guide/")
            field = page.get_by_role("combobox", name="Куда отправимся?")
            button = page.get_by_role("button", name="Найти", exact=True)
            assert button.evaluate("el=>getComputedStyle(el).fontSize") == "22px"
            assert button.evaluate("el=>getComputedStyle(el).backgroundColor") == "rgb(40, 97, 235)"
            field.fill("нов")
            expect(page.get_by_role("option")).to_have_count(6)
            field.press("ArrowDown")
            expect(page.get_by_role("option").first).to_have_attribute("aria-selected", "true")
            assert "Великий Новгород" in page.get_by_role("option").first.inner_text()
            page.wait_for_function("""() => {
                const shell = document.querySelector('.search-surface');
                return !shell.getAnimations().length && shell.offsetHeight > 80;
            }""")
            assert (
                page.locator(".search-surface").evaluate("el=>getComputedStyle(el).borderRadius")
                == "24px"
            )
            page.screenshot(path=str(tmp_path / f"search-{label}.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            field.fill("пле")
            expect(page.get_by_role("option")).to_have_count(1)
            expect(page.get_by_role("button", name="Все результаты")).to_have_count(0)
            button.click()
            page.wait_for_url(lambda u: parse_qs(urlsplit(u).query).get("q") == ["пле"])
            expect(page.locator(".city-link")).to_have_count(1)
            field = page.get_by_role("combobox", name="Куда отправимся?")
            field.fill("плес")
            expect(page.get_by_role("option")).to_have_count(1)
            if width == 390:
                page.get_by_role("option").first.tap()
            else:
                field.press("ArrowDown")
                field.press("Enter")
            page.wait_for_url(origin + "/guide/plyos")
            assert errors == []
            page.close()
        context = browser.new_context(java_script_enabled=False)
        page = context.new_page()
        page.goto(origin + "/guide/")
        page.get_by_label("Куда отправимся?").fill("новгород нижний")
        page.get_by_role("button", name="Найти", exact=True).click()
        expect(page.locator(".city-link")).to_have_count(1)
        expect(page.locator(".city-link")).to_contain_text("Нижний Новгород")
        expect(page.locator(".search-suggestions")).to_be_hidden()
        browser.close()


def test_late_responses_escape_ime_error_and_untrusted_titles(database):
    settings, _ = database
    with serving(create_app(settings)) as origin, sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        page = browser.new_page()
        # Simulate a transport that cannot cancel: generation checks must still reject old replies.
        page.add_init_script(
            "const actualFetch=window.fetch;window.fetch=(url,options)=>actualFetch(url,{...options,signal:undefined});"
        )
        pending, seen = {}, []

        def hold(route):
            q = parse_qs(urlsplit(route.request.url).query)["q"][0]
            assert "cookie" not in route.request.headers
            pending[q] = route
            seen.append(q)

        page.route("**/api/locations/suggest?*", hold)
        page.goto(origin + "/guide/")
        field = page.get_by_role("combobox", name="Куда отправимся?")

        def wait_pending(q):
            for _ in range(100):
                if q in pending:
                    return
                page.wait_for_timeout(10)
            raise AssertionError("Missing suggestion request: " + q)

        def send(q):
            with page.expect_request(lambda r: parse_qs(urlsplit(r.url).query).get("q") == [q]):
                field.fill(q)
            wait_pending(q)

        def complete(q, name="", status=200):
            body = (
                {"items": [{"name": name, "context": "Город · Область", "url": "/guide/ivanovo"}]}
                if name
                else {"items": []}
            )
            with page.expect_response(
                lambda r: parse_qs(urlsplit(r.url).query).get("q") == [q]
            ) as received:
                pending.pop(q).fulfill(
                    status=status, content_type="application/json", body=json.dumps(body)
                )
            received.value.finished()
            page.wait_for_timeout(40)

        send("стар")
        send("нов")
        complete("нов", "Новое место")
        expect(page.get_by_role("option")).to_contain_text("Новое место")
        complete("стар", "Старое место")
        expect(page.get_by_role("option")).to_contain_text("Новое место")
        send("ожид")
        expect(page.locator(".search-message")).to_have_text("Ищем…")
        field.press("ArrowDown")
        assert field.get_attribute("aria-activedescendant") in (None, "")
        complete("ожид", "Ожидаемое место")
        expect(page.get_by_role("option")).to_contain_text("Ожидаемое место")
        send("фокус")
        page.locator("h1").click()
        complete("фокус", "Ответ без фокуса")
        expect(page.get_by_role("option")).to_contain_text("Ответ без фокуса")
        expect(page.locator(".search-suggestions")).to_be_visible()
        field.press("Escape")
        expect(page.locator(".search-suggestions")).to_be_visible()
        expect(field).to_have_value("фокус")
        send("сброс")
        page.get_by_role("button", name="Очистить поиск").click()
        complete("сброс", "Устаревший ответ после очистки")
        expect(page.locator(".search-suggestions")).to_be_hidden()
        expect(field).to_have_value("")
        before = len(seen)
        field.dispatch_event("compositionstart")
        field.fill("моск")
        page.wait_for_timeout(260)
        assert len(seen) == before
        field.dispatch_event("compositionend")
        wait_pending("моск")
        complete("моск", "Москва")
        expect(page.get_by_role("option")).to_contain_text("Москва")
        send("опас")
        complete("опас", '<img src=x onerror="window.__searchXss=true">')
        assert page.locator(".destination-results img").count() == 0
        assert not page.evaluate("Boolean(window.__searchXss)")
        expect(page.get_by_role("option")).to_contain_text("<img")
        send("ошиб")
        complete("ошиб", status=503)
        expect(page.locator(".search-message")).to_have_text("Подсказки временно недоступны.")
        field.press("ArrowDown")
        field.press("Enter")
        page.wait_for_url(re.compile(r"\?q="))
        assert parse_qs(urlsplit(page.url).query)["q"] == ["ошиб"]
        browser.close()


def test_morph_reverses_without_hiding_reopened_panel_and_respects_reduced_motion(database):
    settings, _ = database
    with serving(create_app(settings)) as origin, sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        requests = []

        def suggest(route):
            requests.append(route.request.url)
            route.fulfill(
                json={"items": [{"name": "Новгород", "context": "Город", "url": "/guide/novgorod"}]}
            )

        page.route("**/api/locations/suggest?*", suggest)
        page.goto(origin + "/guide/")
        field = page.get_by_role("combobox", name="Куда отправимся?")
        shell = page.locator(".search-surface")
        panel = page.locator(".search-suggestions")
        city_top = page.locator(".cities").bounding_box()["y"]
        assert shell.evaluate("el=>el.offsetHeight") == 80
        assert shell.evaluate("el=>getComputedStyle(el).borderRadius") == "40px"
        field.fill("н")
        expect(page.get_by_role("option")).to_have_count(1)
        assert len(requests) == 1 and parse_qs(urlsplit(requests[0]).query)["q"] == ["н"]
        field.fill("нов")
        expect(page.get_by_role("option")).to_have_count(1)
        page.wait_for_function("!document.querySelector('.search-surface').getAnimations().length")
        expanded_height = shell.evaluate("el=>el.getBoundingClientRect().height")
        assert expanded_height > 80
        assert page.locator(".cities").bounding_box()["y"] == city_top
        field.press("Escape")
        page.get_by_role("button", name="Найти", exact=True).focus()
        page.wait_for_timeout(480)
        expect(panel).to_be_visible()
        expect(field).to_have_value("нов")
        before = len(requests)
        field.focus()
        page.wait_for_timeout(250)
        assert len(requests) == before
        field.fill("")
        expect(panel).to_have_attribute("inert", "")
        page.wait_for_timeout(80)
        assert 80 < shell.evaluate("el=>el.getBoundingClientRect().height") < expanded_height
        field.fill("новг")
        expect(panel).to_have_attribute("aria-hidden", "false")
        expect(page.get_by_role("option")).to_have_count(1)
        page.wait_for_timeout(480)
        expect(panel).to_be_visible()
        assert shell.evaluate("el=>getComputedStyle(el).borderRadius") == "24px"
        page.emulate_media(reduced_motion="reduce")
        page.get_by_role("button", name="Очистить поиск").click()
        expect(panel).to_be_hidden()
        page.wait_for_function("document.querySelector('.search-surface').offsetHeight === 80")
        assert shell.evaluate("el=>getComputedStyle(el).borderRadius") == "40px"
        browser.close()
