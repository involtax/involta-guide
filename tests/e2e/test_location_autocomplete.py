import json
import os
from urllib.parse import parse_qs, urlsplit

from integration.editor_fixture import FixtureEditor
from integration.test_editorial import CONTENT
from integration.test_location_search import add_location
from integration.test_voice_upload_transport import serving
from playwright.sync_api import expect, sync_playwright

from involta_guide.application.services.locations import Queries
from involta_guide.domain.location_search import search_locations
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
                == "34px"
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
            page.wait_for_url(origin + "/guide/ples")
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
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        page = browser.new_page()
        page.goto(origin + "/guide/", wait_until="networkidle")
        field = page.get_by_role("combobox", name="Куда отправимся?")
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
            const input = document.querySelector('input[name=q]');
            input.value = 'и'; input.dispatchEvent(new Event('input', {bubbles:true}));
            await new Promise(requestAnimationFrame);
            return {names:[...document.querySelectorAll('.result-name')].map(node=>node.textContent),
                busy: input.getAttribute('aria-busy')};
        }""")
        assert "Иваново" in result["names"] and result["busy"] is None
        page.locator("h1").click()
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
        assert not requests
        assert page.evaluate("searchMessages.every(text => !text.includes('Ищем'))")
        browser.close()


def test_browser_and_native_matching_agree_on_real_catalogue_and_five_thousand_rows(
    database, tmp_path
):
    settings, _ = database
    with serving(create_app(settings)) as origin, sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        page = browser.new_page()
        page.goto(origin + "/guide/", wait_until="networkidle")
        catalogue = page.locator("#location-catalogue").evaluate("el=>JSON.parse(el.textContent)")
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
                "([rows,q])=>GuideLocationIndex(rows)(q).map(row=>row.url)", [catalogue, query]
            )
            expected = [row["url"] for row in search_locations(catalogue, query)]
            assert actual == expected, query
        benchmark = page.evaluate("""() => {
            const rows = Array.from({length:5000},(_,i)=>({name:'Город ' + i,context:'Подпись',url:'/guide/city-'+i}));
            rows.push({name:'Иваново',context:'Город',url:'/guide/ivanovo'});
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
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        page.goto(origin + "/guide/")
        field = page.get_by_role("combobox", name="Куда отправимся?")
        shell = page.locator(".search-surface")
        panel = page.locator(".search-suggestions")
        city_top = page.locator(".cities").bounding_box()["y"]
        assert shell.evaluate("el=>el.offsetHeight") == 80
        assert shell.evaluate("el=>getComputedStyle(el).borderRadius") == "40px"
        field.fill("н")
        expect(page.get_by_role("option")).to_have_count(6)

        field.fill("нов")
        expect(page.get_by_role("option")).to_have_count(6)
        page.wait_for_function("!document.querySelector('.search-surface').getAnimations().length")
        expanded_height = shell.evaluate("el=>el.getBoundingClientRect().height")
        assert expanded_height > 80
        assert page.locator(".cities").bounding_box()["y"] == city_top
        field.press("Escape")
        page.get_by_role("button", name="Найти", exact=True).focus()
        page.wait_for_timeout(480)
        expect(panel).to_be_visible()
        expect(field).to_have_value("нов")
        field.focus()
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
