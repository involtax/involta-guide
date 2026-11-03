import copy
import importlib.util
import json
from pathlib import Path

import httpx
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_guide.infrastructure.ai.audit import AIAudit
from involta_guide.infrastructure.jobs.worker import Worker
from involta_guide.main import create_app

from .test_background_generation import configured, envelope, task
from .test_jobs import setup
from .test_web import login, token

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/pereslavl-generation-response.json"


def test_legacy_real_response_reprocess_cleans_site_and_shows_partial_coverage(database):
    raw = json.loads(FIXTURE.read_text())
    original = copy.deepcopy(raw)
    requests = []

    def handler(request):
        requests.append(request.method)
        return httpx.Response(200, json=envelope(content=raw))

    settings, engine, queries, location, service, queue, provider, job = configured(
        database, handler, location_query="Переславль-Залесский"
    )
    try:
        Worker(queue, service).once()
        initial = queries.get("locations", location["id"])["published_version"]
        cached = task(engine, job)["response_json"]
        with engine.begin() as conn:
            # Existing pages from the former parser can still contain raw citation text.
            for card in raw["restaurants"]:
                conn.execute(
                    text("UPDATE restaurants SET description=:description WHERE name=:name"),
                    {"description": card["description"], "name": card["name"]},
                )
        assert "](" in queries.public(location["slug"])["content"]["restaurants"][0]["description"]
        with TestClient(create_app(settings)) as client:
            login(client)
            page = client.get("/guide/logs/" + location["slug"] + "/1")
            assert "Поиск неполный" in page.text
            assert "Бояре" in page.text and "Нужно проверить" in page.text
            response = client.post(
                "/guide/admin/actions/reprocess-generation",
                data={
                    "_csrf": token(page.text),
                    "log_slug": location["slug"],
                    "log_number": 1,
                    "expected": initial,
                },
                headers={"origin": "http://testserver"},
            )
            assert response.status_code == 200
        provider.client.close()
        Worker(queue, service).once()
        assert requests == ["POST"]
        assert raw == original and task(engine, job)["response_json"] == cached
        assert len(queries.rows("restaurants")) == 2
        public = queries.public(location["slug"])["content"]
        assert all("](" not in card["description"] for card in public["restaurants"])
        assert public["qualityReport"]["status"] == "partial"
        assert len(public["candidateReview"]) == 10
        assert len(queries.rows("versions")) == 2
        assert AIAudit(engine, settings).detail(job["id"])["job"]["stage_label"] == "Поиск неполный"
        with TestClient(create_app(settings)) as client:
            login(client)
            for tab in ["locations", "restaurants"]:
                html = client.get(
                    "/guide/admin", params={"tab": tab, "location": location["id"]}
                ).text
                assert "Полнота поиска" in html and "Поиск неполный" in html and "Бояре" in html
            current = queries.get("locations", location["id"])["published_version"]
            intro = client.post(
                "/guide/admin/actions/intro",
                data={
                    "_csrf": token(client.get("/guide/admin?tab=locations").text),
                    "location_id": location["id"],
                    "expected": current,
                    "expected_published": current,
                    "intro": "Авторское вступление",
                },
                headers={"origin": "http://testserver"},
            )
            assert intro.status_code == 200
            assert (
                queries.location(location["id"])["content"]["qualityReport"]["status"] == "partial"
            )
    finally:
        provider.close()


def test_generation_prompt_has_candidate_limit_without_display_limit(database):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=envelope())

    settings, engine, queries, location, service, queue, provider, job = configured(
        database, handler
    )
    try:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE locations SET top_limit=1 WHERE id=:id"), {"id": location["id"]}
            )
        Worker(queue, service).once()
        sent = bodies[0]["input"][1]["content"]
        assert '"candidateLimit": 10' in sent
        assert '"topLimit"' not in sent and "{лимит}" not in sent
        assert "ОБЯЗАТЕЛЬНЫЙ КОНТРАКТ ПОЛНОТЫ" in sent
        assert "candidateReview" in sent
    finally:
        provider.close()


def test_prompt_migration_preserves_custom_edits_and_original_queued_prompt(database):
    settings, engine = database
    legacy = "Авторский стиль и важная география. Размер подборки определяется полем {лимит}, заданным редактором. Твой результат — черновик. Решение о публикации принимает человек."
    with engine.begin() as conn:
        conn.execute(text("UPDATE settings SET value=:value WHERE key='prompt'"), {"value": legacy})
    _, _, _, queries, location, service, _ = setup(database)
    service.enqueue(location["id"])
    queued = queries.rows("jobs")[0]
    with engine.begin() as conn:
        spec = importlib.util.spec_from_file_location(
            "coverage_migration", Path("migrations/versions/0006_generation_coverage.py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            first = conn.execute(text("SELECT value FROM settings WHERE key='prompt'")).scalar_one()
            module.upgrade()
        assert first.startswith("Авторский стиль и важная география.")
        assert "Размер подборки определяется" not in first and "{лимит}" not in first
        assert "Твой результат — черновик" not in first and "candidateReview" in first
        backup = conn.execute(
            text("SELECT value FROM settings WHERE key='prompt_before_coverage_v2'")
        ).scalar_one()
        assert backup == legacy
        assert (
            conn.execute(
                text("SELECT count(*) FROM audit WHERE action='prompt_upgrade'")
            ).scalar_one()
            == 1
        )

    assert queries.get("jobs", queued["id"])["prompt"] == legacy


def test_partial_coverage_is_readable_in_browser_and_on_mobile(database, tmp_path):
    import os

    from playwright.sync_api import expect, sync_playwright

    raw = json.loads(FIXTURE.read_text())

    def handler(request):
        return httpx.Response(200, json=envelope(content=raw))

    settings, engine, queries, location, service, queue, provider, job = configured(
        database, handler, location_query="Переславль-Залесский"
    )
    try:
        Worker(queue, service).once()
        with TestClient(create_app(settings)) as client, sync_playwright() as p:
            login(client)
            browser = p.chromium.launch(
                executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
            )
            page = browser.new_page(viewport={"width": 1440, "height": 1000})

            def respond(route):
                response = client.get(route.request.url)
                route.fulfill(
                    status=response.status_code,
                    body=response.content,
                    content_type=response.headers.get("content-type", "text/plain"),
                )

            page.route("**/*", respond)
            page.goto(
                "http://testserver/guide/admin?tab=restaurants&location=" + location["id"],
                wait_until="networkidle",
            )
            quality = page.get_by_role("region", name="Полнота поиска")
            expect(quality.get_by_role("heading", name="Поиск неполный")).to_be_visible()
            quality.locator("summary").filter(has_text="Все кандидаты").click()
            expect(quality.get_by_text("Бояре", exact=True)).to_be_visible()
            assert "](" not in page.locator(".restaurant-description").first.inner_text()
            page.screenshot(path=tmp_path / "coverage-desktop.png", full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=tmp_path / "coverage-mobile.png", full_page=True)
            browser.close()
    finally:
        provider.close()
