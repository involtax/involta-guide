import copy
import json
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_guide.application.commands.schemas import GeneratedContent
from involta_guide.infrastructure.ai.audit import AIAudit
from involta_guide.infrastructure.jobs.worker import Worker
from involta_guide.main import create_app

from .test_background_generation import configured, envelope, task
from .test_web import login

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/pereslavl-generation-response.json"


def saved_legacy_response(engine, job, raw):
    """A previously submitted/completed response keeps its recorded old schema."""
    usage = str(uuid4())
    request = {
        "model": job["model"],
        "input": [
            {"role": "system", "content": "JSON"},
            {"role": "user", "content": "Сохранённый промпт прежнего запроса"},
        ],
        "max_output_tokens": 15000,
        "text": {"format": {"schema": GeneratedContent.model_json_schema()}},
    }
    response = {
        "id": "resp_saved_pereslavl",
        "status": "completed",
        "output": [{"content": [{"type": "output_text", "text": json.dumps(raw)}]}],
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ai_usage(id,job_id,model,operation,status) VALUES(:id,:job,:model,'responses','done')"
            ),
            {"id": usage, "job": job["id"], "model": job["model"]},
        )
        conn.execute(
            text(
                "INSERT INTO generation_requests(job_id,usage_id,response_id,state,request_json,response_json,deadline_at) VALUES(:job,:usage,'resp_saved_pereslavl','completed',CAST(:request AS jsonb),CAST(:response AS jsonb),now()+interval '1 hour')"
            ),
            {
                "job": job["id"],
                "usage": usage,
                "request": json.dumps(request),
                "response": json.dumps(response),
            },
        )


def test_legacy_real_response_reprocess_cleans_site_and_shows_partial_coverage(database):
    raw = json.loads(FIXTURE.read_text())
    original = copy.deepcopy(raw)
    requests = []

    def handler(request):
        requests.append(request.method)
        raise AssertionError("Сохранённый ответ не должен обращаться к провайдеру")

    settings, engine, queries, location, service, queue, provider, job = configured(
        database, handler, location_query="Переславль-Залесский"
    )
    saved_legacy_response(engine, job, raw)
    try:
        Worker(queue, service).once()
        cached = task(engine, job)["response_json"]
        with TestClient(create_app(settings)) as client:
            login(client)
            page = client.get("/guide/logs/" + location["slug"] + "/1")
            assert "Поиск неполный" in page.text
            assert "Бояре" in page.text and "Нужно проверить" in page.text
            assert "Переобработать тот же ответ" not in page.text
            assert "reset-and-fill" not in page.text
        assert requests == []
        assert raw == original and task(engine, job)["response_json"] == cached
        assert len(queries.rows("restaurants")) == 2
        public = queries.location(location["id"])["content"]
        assert all("](" not in card["description"] for card in public["restaurants"])
        assert public["qualityReport"]["status"] == "partial"
        assert len(public["candidateReview"]) == 10
        assert queries.get("jobs", job["id"])["changes"]
        assert AIAudit(engine, settings).detail(job["id"])["job"]["stage_label"] == "Поиск неполный"
        with TestClient(create_app(settings)) as client:
            login(client)
            for path in [
                "/guide/admin/locations/" + location["slug"],
                "/guide/admin/restaurants/" + location["slug"],
            ]:
                html = client.get(path).text
                assert "Полнота поиска" in html and "Поиск неполный" in html and "Бояре" in html
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
        assert bodies[0]["max_output_tokens"] == settings.generation_max_output_tokens == 32000
        sent = bodies[0]["input"][1]["content"]
        assert '"candidateLimit": 10' in sent
        assert '"topLimit"' not in sent and "{лимит}" not in sent
        assert "Верни только компактный JSON" in sent
        assert "candidates" in sent and "sourceRefs" in sent
        assert "candidateReview" not in sent and "rankingReport" not in sent
        assert "alexStar" not in json.dumps(bodies[0])
    finally:
        provider.close()


def test_partial_coverage_is_readable_in_browser_and_on_mobile(database, tmp_path):
    import os

    from playwright.sync_api import expect, sync_playwright

    raw = json.loads(FIXTURE.read_text())

    def handler(request):
        raise AssertionError("Сохранённый ответ не должен обращаться к провайдеру")

    settings, engine, queries, location, service, queue, provider, job = configured(
        database, handler, location_query="Переславль-Залесский"
    )
    saved_legacy_response(engine, job, raw)
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
                "http://testserver/guide/admin/restaurants/" + location["slug"],
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
