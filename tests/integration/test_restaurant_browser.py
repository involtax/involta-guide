from copy import deepcopy
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_guide.domain.rules import FACTORS
from involta_guide.main import create_app

from .test_editorial import CARD, services
from .test_web import login


def test_restaurant_browser_filters_ranks_and_renders_complete_records(database):
    settings, engine = database
    editor, queries, factory = services(database)
    ivanovo = queries.directory(query="Иваново")["rows"][0]
    kazan = queries.directory(query="Казань")["rows"][0]
    cards = []
    for name, value in [("Неполная оценка", None), ("Нулевая оценка", 0), ("Лидер", 9)]:
        cards.append(
            {
                **deepcopy(CARD),
                "name": name,
                "description": "Полное описание " + name,
                "sources": ["https://example.com/menu"],
                "latitude": 0,
                "longitude": 0,
            }
        )
    report = [
        {
            "name": c["name"],
            "mapsUrl": c["mapsUrl"],
            "scores": dict.fromkeys(FACTORS, value),
            "evidence": [],
        }
        for c, value in zip(cards, [None, 0, 9])
    ]
    editor.save(ivanovo["id"], {"intro": "", "restaurants": cards, "rankingReport": report}, None)
    editor.save(kazan["id"], {"intro": "", "restaurants": [{**CARD, "name": "Казанский"}]}, None)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE versions SET created_at=now()-interval '1 day' WHERE location_id=:id"),
            {"id": ivanovo["id"]},
        )
    result = queries.restaurant_browser()
    assert [c["id"] for c in result["restaurant_cities"]] == [kazan["id"], ivanovo["id"]]
    assert result["restaurant_city"]["id"] == kazan["id"]
    assert len(queries.restaurant_browser("иван")["restaurant_cities"]) == 1
    assert queries.restaurant_browser("%")["restaurant_cities"] == []
    assert queries.restaurant_browser("несуществующий")["restaurant_city"] is None
    result = queries.restaurant_browser(location_id=ivanovo["id"])
    assert [c["name"] for c in result["restaurant_cards"]] == [
        "Лидер",
        "Нулевая оценка",
        "Неполная оценка",
    ]
    leader = result["restaurant_cards"][0]
    # Pending/fraud reviews must never affect visitor ratings.
    with factory() as uow:
        for moderation, fraud, rating in [
            ("approved", 0, 7),
            ("pending", 0, 10),
            ("approved", 1, 1),
        ]:
            uow.repo.add(
                "reviews",
                {
                    "id": str(uuid4()),
                    "location_id": ivanovo["id"],
                    "visitor": str(uuid4()),
                    "audio": "test.webm",
                    "mime": "audio/webm",
                    "hash": "test",
                    "ip_hash": "test",
                    "device": "test",
                    "status": "processed",
                    "moderation": moderation,
                    "fraud": fraud,
                    "result": {
                        "entries": [
                            {
                                "restaurantId": leader["id"],
                                "restaurantName": "Лидер",
                                "rating": rating,
                            }
                        ]
                    },
                },
            )
    result = queries.restaurant_browser(location_id=ivanovo["id"])
    assert result["restaurant_cards"][0]["review_scores"]["rating"] == 7
    with TestClient(create_app(settings)) as client:
        assert "Полное описание" not in client.get("/guide/admin?tab=restaurants").text
        login(client)
        response = client.get(
            "/guide/admin", params={"tab": "restaurants", "location": ivanovo["id"]}
        )
        assert response.status_code == 200
        assert "restaurant-sidebar" in response.text and 'aria-current="page"' in response.text
        assert response.text.index("<h3>Лидер</h3>") < response.text.index(
            "<h3>Нулевая оценка</h3>"
        )
        assert (
            "Полное описание Лидер" in response.text and "https://example.com/menu" in response.text
        )
        assert "0.0, 0.0" in response.text and "7.00" in response.text
        fragment = client.get("/guide/admin?tab=restaurants&q=иван", headers={"HX-Request": "true"})
        assert fragment.status_code == 200 and "<!doctype" not in fragment.text
        assert "Казанский" not in fragment.text
        assert client.get("/guide/admin?tab=restaurants&q=пустой").status_code == 200


def test_restaurant_browser_in_chromium(database, tmp_path):
    import os

    from playwright.sync_api import sync_playwright

    settings, _ = database
    editor, queries, _ = services(database)
    city = queries.directory(query="Иваново")["rows"][0]
    editor.save(
        city["id"],
        {
            "intro": "",
            "restaurants": [
                {
                    **CARD,
                    "openingHours": {
                        "timezone": "Europe/Moscow",
                        "week": [[["10:00", "23:00"]]] * 7,
                        "exceptions": {"2026-12-31": [["12:00", "02:00"]]},
                    },
                    "sources": ["https://example.com/menu"],
                }
            ],
        },
        None,
    )
    second_city = queries.directory(query="Казань")["rows"][0]
    editor.save(
        second_city["id"],
        {"intro": "", "restaurants": [{**CARD, "name": "Казанский ресторан"}]},
        None,
    )
    with TestClient(create_app(settings)) as client, sync_playwright() as p:
        login(client)
        browser = p.chromium.launch(
            executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
        )
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def respond(route):
            headers = {"HX-Request": "true"} if route.request.headers.get("hx-request") else {}
            response = client.get(route.request.url, headers=headers)
            route.fulfill(
                status=response.status_code,
                body=response.content,
                content_type=response.headers.get("content-type", "text/plain"),
            )

        page.route("**/*", respond)
        page.goto(
            "http://testserver/guide/admin?tab=restaurants&location=" + city["id"],
            wait_until="networkidle",
        )
        assert page.locator(".restaurant-record").count() == 1
        assert (
            page.locator(".restaurant-sidebar").bounding_box()["x"]
            < page.locator(".restaurant-results").bounding_box()["x"]
        )
        assert "10:00–23:00" in page.locator(".restaurant-hours").inner_text()
        page.screenshot(path=tmp_path / "restaurant-desktop.png", full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path=tmp_path / "restaurant-mobile.png", full_page=True)
        page.locator(".restaurant-city", has_text="Казань").click()
        page.wait_for_selector(".restaurant-record h3:text('Казанский ресторан')")
        page.locator(".restaurant-city", has_text="Иваново").click()
        page.wait_for_selector(".restaurant-hours")
        # HTMX attaches the new input handlers during its settle phase after the swap.
        page.wait_for_load_state("networkidle")
        page.locator("#restaurant-city-search").fill("ничего")
        page.wait_for_selector(".restaurant-record", state="detached")
        assert "наполненных городов нет" in page.locator(".restaurant-sidebar").inner_text()
        assert not errors
        browser.close()
