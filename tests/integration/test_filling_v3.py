import json
from pathlib import Path
from uuid import uuid4

import httpx
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text

from involta_guide.application.commands.schemas import GeneratedContent
from involta_guide.infrastructure.ai.audit import AIAudit
from involta_guide.infrastructure.ai.openai import OpenAIProvider
from involta_guide.infrastructure.jobs.worker import Worker
from involta_guide.main import create_app

from .editor_fixture import enqueue_fixture
from .test_background_generation import configured, envelope
from .test_jobs import FakeAI, setup
from .test_web import login

FIXTURE = Path(__file__).parents[1] / "fixtures/novgorod-incomplete-response.json"


def test_real_novgorod_truncation_is_visible_and_never_retried_or_published(database):
    calls = []
    response = json.loads(FIXTURE.read_text())

    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json=response)

    settings, engine, queries, location, service, queue, provider, job = configured(
        database, handler, location_query="Великий Новгород"
    )
    provider.settings = settings.model_copy(update={"generation_max_output_tokens": 15000})
    try:
        assert Worker(queue, service).once()
        assert not Worker(queue, service).once()
        detail = AIAudit(engine, settings).detail(job["id"])
        assert detail["diagnostics"]["max_output_tokens"] == 15000
        assert detail["diagnostics"]["output_tokens"] == 15000
        assert detail["diagnostics"]["reasoning_tokens"] == 3395
        assert detail["diagnostics"]["incomplete_reason"] == "max_output_tokens"
        assert not detail["job"]["can_reprocess"] and not detail["job"]["can_resume"]
        assert queries.get("locations", location["id"])["initialized_at"] is None
        assert queries.rows("restaurants") == []
        assert calls == ["POST"]
        assert "estimated_cost_usd" not in detail["calls"][0]
        with TestClient(create_app(settings)) as client:
            page = client.get("/guide/logs/veliky-novgorod/1")
            assert page.status_code == 200
            assert "Диагностика ответа" in page.text
            assert "Нужен новый запрос" in page.text and "3395" in page.text
        import os

        from playwright.sync_api import sync_playwright

        with TestClient(create_app(settings)) as client, sync_playwright() as playwright:
            login(client)
            browser = playwright.chromium.launch(
                executable_path=os.getenv("CHROMIUM_PATH") or None, args=["--no-sandbox"]
            )
            page = browser.new_page(viewport={"width": 1440, "height": 1100})

            def respond(route):
                response = client.get(route.request.url)
                route.fulfill(
                    status=response.status_code,
                    body=response.content,
                    headers=dict(response.headers),
                )

            page.route("**/*", respond)
            page.goto("http://testserver/guide/logs/veliky-novgorod/1", wait_until="networkidle")
            assert page.get_by_role("region", name="Диагностика ответа").count() == 1
            shots = Path("var/screenshots")
            shots.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=shots / "filling-v3-log-desktop.png")
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.get_by_role("region", name="Диагностика ответа").screenshot(
                path=shots / "filling-v3-log-mobile.png"
            )
            browser.close()
    finally:
        provider.close()


def test_contract_upgrade_preserves_submitted_requests_history_and_admin_prompt_edits(
    database, monkeypatch, capsys
):
    settings, engine, uow, queries, city, service, queue = setup(database)
    config = Config(str(settings.root_dir / "alembic.ini"))
    # Exercise the historical prompt chain without reversing the later,
    # intentionally irreversible numeric-identity migration in this fixture.
    command.stamp(config, "restaurant_discovery")
    command.downgrade(config, "readable_slugs")
    command.stamp(config, "audience_stats")
    command.downgrade(config, "canonical_rank")
    old_prompt = "Сохранённый старый промпт до смены контракта. " * 2
    with uow() as tx:
        tx.repo.set_setting("prompt", old_prompt)
    enqueue_fixture(service, city["id"])
    other = queries.directory(query="Шуя")["rows"][0]
    enqueue_fixture(service, other["id"])
    submitted = next(j for j in queries.rows("jobs") if j["location_id"] == city["id"])
    untouched = next(j for j in queries.rows("jobs") if j["location_id"] == other["id"])
    old_request = {
        "model": submitted["model"],
        "input": [{"role": "system", "content": "JSON"}, {"role": "user", "content": old_prompt}],
        "max_output_tokens": 15000,
        "text": {"format": {"schema": GeneratedContent.model_json_schema()}},
    }
    usage_id = str(uuid4())
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ai_usage(id,job_id,model,operation,status,estimated_cost_usd) VALUES(:id,:job,:model,'responses','reserved',0.75)"
            ),
            {"id": usage_id, "job": submitted["id"], "model": submitted["model"]},
        )
        conn.execute(
            text(
                "INSERT INTO generation_requests(job_id,usage_id,response_id,state,request_json,deadline_at) VALUES(:job,:usage,'resp_old_contract','queued',CAST(:body AS jsonb),now()+interval '1 hour')"
            ),
            {"job": submitted["id"], "usage": usage_id, "body": json.dumps(old_request)},
        )
    from involta_guide.infrastructure.persistence import release_backup

    monkeypatch.setattr("sys.argv", ["release_backup", "needed"])
    release_backup.main()
    assert capsys.readouterr().out.strip() == "yes"
    command.upgrade(config, "restaurant_discovery")
    new_prompt = (
        ScriptDirectory.from_config(config).get_revision("restaurant_discovery").module.PROMPT
    )
    assert queries.setting("prompt") == new_prompt
    assert queries.get("jobs", untouched["id"])["prompt"] == new_prompt
    assert queries.get("jobs", submitted["id"])["prompt"] == old_prompt
    calls = []

    # envelope() normally models new HTTP output; preserve the actual old contract here.
    def legacy_handler(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": "resp_old_contract",
                    "status": "completed",
                    "usage": {"input_tokens": 50, "output_tokens": 100},
                    "output": [
                        {
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": json.dumps(FakeAI().generate("", "")),
                                }
                            ]
                        }
                    ],
                },
            )
        return httpx.Response(200, json=envelope())

    provider = OpenAIProvider(
        settings.model_copy(update={"openai_api_key": SecretStr("fixture-key")}), engine
    )
    provider.client.close()
    provider.client = httpx.Client(transport=httpx.MockTransport(legacy_handler))
    service.provider = provider
    try:
        assert Worker(queue, service).once() and Worker(queue, service).once()
        assert calls == ["GET", "POST"]
        assert all(j["applied"] for j in queries.rows("jobs"))
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT estimated_cost_usd FROM ai_usage WHERE id=:id"), {"id": usage_id}
                ).scalar_one()
                == 0.75
            )
            assert (
                conn.execute(
                    text("SELECT estimated_cost_usd FROM ai_usage WHERE id<>:id"), {"id": usage_id}
                ).scalar_one()
                is None
            )
            recorded = conn.execute(
                text("SELECT request_json FROM generation_requests WHERE job_id=:job"),
                {"job": submitted["id"]},
            ).scalar_one()
            assert recorded == old_request
        with uow() as tx:
            tx.repo.set_setting("prompt", "Ручной новый стиль после перехода. " * 3)
        command.upgrade(config, "restaurant_discovery")
        assert queries.setting("prompt").startswith("Ручной новый стиль")
    finally:
        provider.close()
