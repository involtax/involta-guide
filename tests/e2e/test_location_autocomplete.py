import json
import os

import pytest
from integration.editor_fixture import FixtureEditor
from integration.test_editorial import CONTENT
from integration.test_location_search import add_location
from integration.test_voice_upload_transport import serving
from playwright.sync_api import expect, sync_playwright

from involta_guide.application.services.locations import Queries
from involta_guide.domain.location_search import search_locations
from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow
from involta_guide.infrastructure.security import Security
from involta_guide.main import create_app

pytestmark = pytest.mark.full_catalog


def test_picker_button_keyboard_and_touch_never_open_a_results_page(database, tmp_path):
    settings, engine = database

    def factory():
        return PostgresUow(engine)

    city = Queries(factory).directory(query="Великий Новгород")["rows"][0]
    FixtureEditor(factory).save(city["id"], CONTENT, None)
    with serving(create_app(settings)) as origin, sync_playwright() as p:
        settings.site_url = origin + settings.base_path
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
            field = page.get_by_role("combobox", name="Поиск локаций и ресторанов")
            button = page.get_by_role("button", name="Выбери", exact=True)
            assert button.evaluate("el=>getComputedStyle(el).fontSize") == "21px"
            assert button.evaluate("el=>el.scrollWidth<=el.clientWidth")
            assert field.bounding_box()["width"] >= 70
            assert field.get_attribute("name") is None
            assert field.evaluate("el=>el.form===null")
            button.click()
            expect(page.get_by_role("option")).to_have_count(8)
            expect(button).to_have_attribute("aria-expanded", "true")
            expect(field).to_have_value("")
            field.press("Enter")
            assert page.url == origin + "/guide/"
            field.fill("нов")
            expect(page.get_by_role("option")).to_have_count(8)
            field.press("ArrowDown")
            expect(page.get_by_role("option").first).to_have_attribute("aria-selected", "true")
            assert "Великий Новгород" in page.get_by_role("option").first.inner_text()
            page.wait_for_function(
                "!document.querySelector('.search-surface').getAnimations().length"
            )
            page.screenshot(path=str(tmp_path / f"picker-{label}.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            field.fill("плес")
            expect(page.get_by_role("option")).to_have_count(1)
            field.press("Enter")
            assert page.url == origin + "/guide/"
            if width < 650:
                page.get_by_role("option").first.tap()
            else:
                field.press("ArrowDown")
                field.press("Enter")
            page.wait_for_url(origin + "/guide/ples")
            assert not errors, errors
            page.close()
        context = browser.new_context(java_script_enabled=False)
        page = context.new_page()
        page.goto(origin + "/guide/")
        page.get_by_label("Поиск локаций и ресторанов").fill("Иваново")
        page.get_by_role("button", name="Выбери", exact=True).click()
        assert page.url == origin + "/guide/"
        expect(page.locator(".cities")).to_have_count(0)
        browser.close()


def test_local_names_are_ready_before_next_frame_offline_and_do_not_search_metadata(database):
    settings, engine = database
    add_location(
        engine,
        "Иной город",
        "uniqueslug777",
        region="ТолькоРегионУникум777",
        aliases=["ТолькоАлиасУникум777"],
    )
    add_location(engine, '</script><img src=x onerror="window.__searchXss=true">', "unsafe-name")
    with serving(create_app(settings)) as origin, sync_playwright() as p:
        settings.site_url = origin + settings.base_path
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        page = browser.new_page()
        page.goto(origin + "/guide/", wait_until="networkidle")
        field = page.get_by_role("combobox", name="Поиск локаций и ресторанов")
        expect(field).to_be_visible()
        page.evaluate("""() => {
            window.searchMessages = [];
            new MutationObserver(() => window.searchMessages.push(document.querySelector('.search-suggestions').textContent))
                .observe(document.querySelector('.search-suggestions'), {subtree:true,childList:true,characterData:true});
        }""")
        requests = []
        page.on("request", lambda request: requests.append(request.url))
        page.context.set_offline(True)
        result = page.evaluate("""async () => {
            const input = document.querySelector('[data-location-input]');
            input.focus(); input.value = 'и'; input.dispatchEvent(new Event('input', {bubbles:true}));
            await new Promise(requestAnimationFrame);
            return {names:[...document.querySelectorAll('.result-name')].map(node=>node.textContent),
                busy: input.getAttribute('aria-busy')};
        }""")
        assert "Иваново" in result["names"] and result["busy"] is None
        page.locator("h1").click()
        expect(page.locator(".search-suggestions")).to_be_hidden()
        expect(field).to_have_value("и")
        field.focus()
        expect(page.locator(".search-suggestions")).to_be_visible()
        for query in ("ТолькоРегионУникум777", "ТолькоАлиасУникум777", "uniqueslug777"):
            field.fill(query)
            expect(page.get_by_role("option")).to_have_count(0)
        field.fill("img")
        expect(page.get_by_role("option")).to_have_count(1)
        assert page.locator(".destination-results img").count() == 0
        assert not page.evaluate("Boolean(window.__searchXss)")
        field.dispatch_event("compositionstart")
        field.fill("москва")
        expect(page.get_by_role("option")).to_contain_text("<img")
        field.dispatch_event("compositionend")
        expect(page.get_by_role("option").first).to_contain_text("Москва")
        field.press("Escape")
        expect(field).to_have_value("москва")
        expect(page.locator(".search-suggestions")).to_be_visible()
        page.get_by_role("button", name="Очистить поиск").click()
        expect(page.locator(".search-suggestions")).to_be_hidden()
        assert all("/api/search" in url for url in requests)
        assert page.evaluate("searchMessages.every(text => !text.includes('Ищем'))")
        browser.close()


def test_browser_and_admin_matching_agree_on_real_catalogue_and_five_thousand_rows(
    database, tmp_path
):
    settings, _ = database
    with serving(create_app(settings)) as origin, sync_playwright() as p:
        settings.site_url = origin + settings.base_path
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        page = browser.new_page()
        with page.expect_response("**/api/locations/catalogue") as catalogue_response:
            page.goto(origin + "/guide/", wait_until="networkidle")
        catalogue = catalogue_response.value.json()
        queries = [
            "и",
            "н",
            "но",
            "нов",
            "ПЛЁС",
            "новгород нижний",
            "cjxb",
            "Сочии",
            "sochi",
            "%_",
            "жцфыщбъь",
            "санкт—петербург",
        ]
        for query in queries:
            actual = page.evaluate(
                "([rows,q])=>GuideLocationIndex(rows)(q).map(row=>row.location)", [catalogue, query]
            )
            expected = [row["location"] for row in search_locations(catalogue, query)]
            assert actual == expected, query
        benchmark = page.evaluate("""() => {
            const rows = Array.from({length:5000},(_,i)=>({name:'Город ' + i,context:'Подпись',location:'city-'+i}));
            rows.push({name:'Иваново',context:'',location:'ivanovo'});
            const started=performance.now(),search=GuideLocationIndex(rows),build=performance.now()-started;
            const times=[];
            for(let i=0;i<100;i++) {const start=performance.now(); search(i%2 ? 'и' : 'город 12');times.push(performance.now()-start);}
            times.sort((a,b)=>a-b);
            return {rows:rows.length,build_ms:build,median_ms:times[50],p95_ms:times[95],found:search('и')[0].name};
        }""")
        assert benchmark["found"] == "Иваново"
        (tmp_path / "local-index-benchmark.json").write_text(json.dumps(benchmark))
        browser.close()


def test_morph_reverses_without_hiding_reopened_panel_and_respects_reduced_motion(database):
    settings, _ = database
    with serving(create_app(settings)) as origin, sync_playwright() as p:
        settings.site_url = origin + settings.base_path
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        page.goto(origin + "/guide/")
        field = page.get_by_role("combobox", name="Поиск локаций и ресторанов")
        shell = page.locator(".search-surface")
        panel = page.locator(".search-suggestions")
        city_top = page.locator(".home-reviews").bounding_box()["y"]
        assert shell.evaluate("el=>el.offsetHeight") == 80
        assert shell.evaluate("el=>getComputedStyle(el).borderRadius") == "40px"
        field.fill("н")
        expect(page.get_by_role("option")).to_have_count(8)

        field.fill("нов")
        expect(page.get_by_role("option")).to_have_count(8)
        page.wait_for_function("!document.querySelector('.search-surface').getAnimations().length")
        expanded_height = shell.evaluate("el=>el.getBoundingClientRect().height")
        assert expanded_height > 80
        assert page.locator(".home-reviews").bounding_box()["y"] == city_top
        field.press("Escape")
        expect(panel).to_be_visible()
        page.get_by_role("button", name="Выбери", exact=True).focus()
        expect(panel).to_be_hidden()
        expect(field).to_have_value("нов")
        field.focus()
        expect(panel).to_be_visible()
        page.wait_for_function("!document.querySelector('.search-surface').getAnimations().length")
        field.fill("")
        expect(panel).to_have_attribute("inert", "")
        page.wait_for_timeout(80)
        assert 80 < shell.evaluate("el=>el.getBoundingClientRect().height") < expanded_height
        field.fill("новг")
        expect(panel).to_have_attribute("aria-hidden", "false")
        expect(page.get_by_role("option")).to_have_count(2)
        page.wait_for_timeout(480)
        expect(panel).to_be_visible()
        assert shell.evaluate("el=>getComputedStyle(el).borderRadius") == "34px"
        page.emulate_media(reduced_motion="reduce")
        page.get_by_role("button", name="Очистить поиск").click()
        expect(panel).to_be_hidden()
        page.wait_for_function("document.querySelector('.search-surface').offsetHeight === 80")
        assert shell.evaluate("el=>getComputedStyle(el).borderRadius") == "40px"
        browser.close()


def test_one_catalogue_request_uses_latest_input_and_failure_retries_without_navigation(database):
    settings, _ = database
    with serving(create_app(settings)) as origin, sync_playwright() as p:
        settings.site_url = origin + settings.base_path
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        for final_query in ("москва", ""):
            page = browser.new_page()
            held = []
            page.route("**/api/locations/catalogue", lambda route: held.append(route))
            page.goto(origin + "/guide/")
            field = page.get_by_label("Поиск локаций и ресторанов")
            field.fill("иван")
            field.fill(final_query)
            page.locator("h1").click()
            for _ in range(100):
                if held:
                    break
                page.wait_for_timeout(10)
            assert len(held) == 1
            assert "Ищем" not in page.locator(".search-suggestions").text_content()
            held[0].fulfill(json=[{"name": "Москва", "context": "", "location": "moskva"}])
            expect(page.get_by_role("combobox")).to_be_visible()
            expect(page.locator(".search-suggestions")).to_be_hidden()
            field.focus()
            if final_query:
                expect(page.get_by_role("option")).to_have_count(1)
                expect(page.get_by_role("option")).to_contain_text("Москва")
                field.fill("мо")
                expect(page.get_by_role("option")).to_have_count(1)
            else:
                expect(page.locator(".search-suggestions")).to_be_hidden()
            assert len(held) == 1
            page.close()
        page = browser.new_page()
        attempts = []

        def retry(route):
            attempts.append(route.request.url)
            if len(attempts) == 1:
                route.fulfill(status=503)
            else:
                route.fulfill(
                    json=[{"name": "Плёс", "context": "Ивановская область", "location": "ples"}]
                )

        page.route("**/api/locations/catalogue", retry)
        page.goto(origin + "/guide/", wait_until="networkidle")
        field = page.get_by_role("combobox", name="Поиск локаций и ресторанов")
        field.fill("плес")
        expect(page.locator(".search-message")).to_contain_text("Не удалось загрузить")
        assert page.url == origin + "/guide/" and len(attempts) == 1
        page.get_by_role("button", name="Выбери", exact=True).click()
        expect(page.get_by_role("option")).to_have_count(1)
        assert len(attempts) == 2
        field.press("ArrowDown")
        field.press("Enter")
        page.wait_for_url(origin + "/guide/ples")
        browser.close()


def test_shared_admin_picker_posts_ids_and_remounts_after_htmx(database):
    settings, engine = database
    choices = [
        ("Локация Выбора Один", "picker-admin-one"),
        ("Локация Выбора Два", "picker-admin-two"),
    ]
    ids = [add_location(engine, name, slug) for name, slug in choices]

    def factory():
        return PostgresUow(engine)

    token = Security(settings, factory).login("test-password-123456", "test-ip")
    with serving(create_app(settings)) as origin, sync_playwright() as p:
        settings.site_url = origin + settings.base_path
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        context = browser.new_context()
        context.add_cookies([{"name": "guide_admin", "value": token, "url": origin}])
        page = context.new_page()
        errors, requests = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: requests.append(request.url))
        page.goto(origin + "/guide/admin/locations/ivanovo")
        for name, slug in choices:
            field = page.get_by_role("combobox", name="Добавить локацию")
            field.fill(name)
            option = page.locator(".destination-results").get_by_role("option")
            expect(option).to_have_count(1)
            with page.expect_request(
                lambda request: (
                    request.method == "POST" and request.url.endswith("/ivanovo/nearby/add")
                )
            ):
                option.click()
            expect(page.locator(".nearby-list").get_by_text(name, exact=True)).to_be_visible()
        city = Queries(factory).admin_location("ivanovo")["location"]
        assert set(ids).issubset(city["nearby_location_ids"])
        assert not any("?q=" in url or "/api/locations/catalogue" in url for url in requests)
        assert errors == []
        browser.close()
