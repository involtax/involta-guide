import json
import os
import re
from copy import deepcopy

from integration.editor_fixture import FixtureEditor
from integration.test_editorial import CONTENT
from integration.test_hybrid_search import public_restaurant
from integration.test_location_search import add_location
from integration.test_voice_upload_transport import serving
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import text

from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow
from involta_guide.main import create_app


def search_cities(engine):
    for name, slug, region in [
        ("Пушкин", "search-pushkin", "Санкт-Петербург"),
        ("Пушкино", "search-pushkino", "Московская область"),
        ("Пушкинские Горы", "search-pushkin-gory", "Псковская область"),
    ]:
        add_location(engine, name, slug, region=region)


def test_two_waves_debounce_stale_answers_and_cache(database, tmp_path):
    settings, engine = database
    public_restaurant(settings, engine, name="Пушкин")
    search_cities(engine)
    with serving(create_app(settings)) as origin, sync_playwright() as p:
        settings.site_url = origin + settings.base_path
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        page = browser.new_page(viewport={"width": 390, "height": 1000})
        errors, held = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/api/search", lambda route: held.append(route))
        page.goto(origin + "/guide/", wait_until="networkidle")
        field = page.get_by_role("combobox", name="Поиск локаций и ресторанов")
        field.press_sequentially("Пушкин", delay=35)
        assert not held
        expect(page.locator(".result-name")).to_contain_text(["Пушкин", "Пушкинские Горы"])
        page.wait_for_function("document.querySelector('.search-surface').offsetHeight > 80")
        page.wait_for_timeout(350)
        assert len(held) == 1
        assert json.loads(held[0].request.post_data)["query"] == "Пушкин"
        # Complete the older request after the input has already changed.
        old = held[0].fetch()
        field.fill("Иваново")
        expect(page.locator(".result-name").first).to_have_text("Иваново")
        try:
            held[0].fulfill(response=old)
        except Exception:
            pass  # The browser may already have cancelled the obsolete transport.
        page.wait_for_timeout(350)
        assert len(held) == 2
        held[1].fulfill(response=held[1].fetch())
        expect(page.locator(".result-name").first).to_have_text("Иваново")
        field.fill("Пушкин")
        page.wait_for_timeout(350)
        assert len(held) == 3
        held[2].fulfill(response=held[2].fetch())
        expect(page.locator(".result-context", has_text=re.compile(r"^Иваново$"))).to_be_visible()
        assert page.locator(".destination-results").get_by_role("option").count() <= 8
        page.wait_for_function("!document.querySelector('.search-surface').getAnimations().length")
        page.screenshot(path=str(tmp_path / "hybrid-mobile.png"), full_page=True)
        for label, width in [("desktop", 1440), ("small-mobile", 320)]:
            page.set_viewport_size({"width": width, "height": 1000})
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            page.screenshot(path=str(tmp_path / f"hybrid-{label}.png"), full_page=True)
        field.fill("Иваново")
        page.wait_for_timeout(350)
        assert len(held) == 3  # This successful query is already cached.
        assert not errors, errors
        browser.close()


def test_fast_local_click_is_logged_and_background_failure_keeps_local_results(database):
    settings, engine = database
    with serving(create_app(settings)) as origin, sync_playwright() as p:
        settings.site_url = origin + settings.base_path
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        page = browser.new_page()
        searches = []
        page.on(
            "request",
            lambda request: (
                searches.append(request) if request.url.endswith("/api/search") else None
            ),
        )
        page.goto(origin + "/guide/", wait_until="networkidle")
        field = page.get_by_role("combobox", name="Поиск локаций и ресторанов")
        field.fill("и")
        page.get_by_role("option", name="Иваново Ивановская область").click()
        page.wait_for_url(origin + "/guide/ivanovo")
        page.wait_for_timeout(100)
        assert not searches
        with engine.connect() as connection:
            row = (
                connection.execute(
                    text("SELECT query,clicked_id,server_state,shown_version FROM search_attempts")
                )
                .mappings()
                .one()
            )
            assert row["query"] == "и" and row["clicked_id"] == "location:ivanovo"
            assert row["server_state"] == "not_requested"
            assert row["shown_version"] == "local-names-v1"
        page.goto(origin + "/guide/", wait_until="networkidle")
        page.route("**/api/search", lambda route: route.fulfill(status=503))
        field.fill("иван")
        expect(page.get_by_role("option").filter(has_text="Иваново")).to_have_count(1)
        page.wait_for_timeout(400)
        expect(page.get_by_role("option").filter(has_text="Иваново")).to_have_count(1)
        assert page.url == origin + "/guide/"
        browser.close()


def test_keyboard_selection_does_not_jump_and_restaurant_anchor_opens_details(database):
    settings, engine = database
    city, card = public_restaurant(settings, engine, name="Пушкин")
    search_cities(engine)

    # Force the search target into the publicly available, initially folded tail.
    def factory():
        return PostgresUow(engine)

    content = deepcopy(CONTENT)
    content["restaurants"] = [
        {**CONTENT["restaurants"][0], "name": name} for name in ["Альфа", "Бета", "Пушкин"]
    ]
    from involta_guide.application.services.locations import Queries

    revision = Queries(factory).admin_location("ivanovo")["location"]["revision"]
    FixtureEditor(factory).save(city["id"], content, revision)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE locations SET top_limit=1 WHERE id=:id"), {"id": city["id"]}
        )
        card_id = str(
            connection.execute(
                text(
                    "SELECT id FROM restaurants WHERE location_id=:id AND name='Пушкин' AND active=1"
                ),
                {"id": city["id"]},
            ).scalar_one()
        )
    with serving(create_app(settings)) as origin, sync_playwright() as p:
        settings.site_url = origin + settings.base_path
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        page = browser.new_page()
        held = []
        page.route("**/api/search", lambda route: held.append(route))
        page.goto(origin + "/guide/", wait_until="networkidle")
        field = page.get_by_role("combobox", name="Поиск локаций и ресторанов")
        field.fill("Пушкин")
        field.press("ArrowDown")
        selected = page.get_by_role("option", selected=True).text_content()
        page.wait_for_timeout(350)
        held[0].fulfill(response=held[0].fetch())
        expect(page.get_by_role("option", selected=True)).to_have_text(selected)
        expect(page.locator(".result-context", has_text=re.compile(r"^Иваново$"))).to_be_visible()
        page.locator(
            ".destination-result",
            has=page.locator(".result-context", has_text=re.compile(r"^Иваново$")),
        ).click()
        page.wait_for_url(origin + "/guide/ivanovo#restaurant-" + card_id)
        target = page.locator("#restaurant-" + card_id)
        expect(target).to_be_visible()
        expect(page.locator(".more-restaurants")).to_have_attribute("open", "")
        browser.close()
