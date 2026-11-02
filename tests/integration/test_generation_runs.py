import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from involta_guide.domain.entities import Conflict
from involta_guide.infrastructure.ai.audit import AIAudit
from involta_guide.infrastructure.ai.openai import OpenAIProvider
from involta_guide.infrastructure.jobs.worker import Worker
from involta_guide.infrastructure.storage.audio import FileAudioStorage
from involta_guide.main import create_app

from .test_jobs import FakeAI, setup
from .test_web import login


def test_model_snapshot_changes_generation_only_and_records_actual_model(database):
    settings, engine, uow, queries, location, service, queue = setup(database)
    settings = settings.model_copy(update={"openai_api_key": SecretStr("fake-no-paid-calls")})
    provider = OpenAIProvider(
        settings, engine, FileAudioStorage(settings.data_dir, settings.max_audio_bytes)
    )
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "resp_model_snapshot",
                "status": "completed",
                "output": [
                    {
                        "content": [
                            {"type": "output_text", "text": json.dumps(FakeAI().generate("", ""))}
                        ]
                    }
                ],
            },
        )

    provider.client.close()
    provider.client = httpx.Client(transport=httpx.MockTransport(handler))
    service.provider = provider
    service.set_generation_model("gpt-5")
    service.enqueue_public_fill(location["id"])
    job = queries.rows("jobs")[0]
    service.set_generation_model("gpt-4.1")
    try:
        assert Worker(queue, service).once()
        assert queries.get("jobs", job["id"])["status"] == "done"
        version = queries.rows("versions")[0]
        assert version["model"] == "gpt-5"
        assert queries.rows("ai_usage")[0]["model"] == "gpt-5"
        provider.analyze("Analyze independently", job["id"])
        assert [r["model"] for r in captured] == ["gpt-5", settings.openai_model]
        assert provider.model == settings.openai_model
    finally:
        provider.close()


def test_manual_repeat_creates_new_log_and_draft_without_replacing_published(database):
    settings, engine, uow, queries, location, service, queue = setup(database)
    service.enqueue_public_fill(location["id"])
    first = queries.rows("jobs")[0]
    assert Worker(queue, service).once()
    published = queries.get("locations", location["id"])["published_version"]
    service.set_generation_model("gpt-5")
    prompt = "Новый промт для повторного наполнения: проверить источники и телефоны."
    service.set_prompt(prompt)
    new_id = service.regenerate(first["id"])
    assert new_id != first["id"]
    assert queries.get("jobs", first["id"])["status"] == "done"
    assert queries.get("jobs", new_id)["prompt"] == prompt
    assert Worker(queue, service).once()
    location_after = queries.get("locations", location["id"])
    assert location_after["published_version"] == published
    assert location_after["draft_version"] != published
    audit = AIAudit(engine, settings)
    assert audit.resolve("ivanovo", 1, public_only=True) == first["id"]
    assert audit.resolve("ivanovo", 2, public_only=True) == new_id
    detail = audit.detail(new_id)
    assert detail["job"]["model_name"] == "gpt-5"
    assert detail["job"]["generation_config"]["previous_number"] == 1
    assert detail["versions"][0]["id"] == location_after["draft_version"]
    assert detail["versions"][0]["model"] == "gpt-5"
    with TestClient(create_app(settings)) as client:
        assert client.get("/guide/logs/ivanovo/2").status_code == 200
        assert client.get("/guide/logs/ivanovo/2.json").json()["job"]["id"] == new_id
        legacy = client.get("/guide/logs/" + first["id"], follow_redirects=False)
        assert legacy.status_code == 308 and legacy.headers["location"] == "/guide/logs/ivanovo/1"
        login(client)
        page = client.get("/guide/admin?tab=fill")
        assert 'value="gpt-5"' in page.text and "Повторить новым запросом" in page.text
        assert "Сохранённая модель: gpt-5" in page.text
        assert 'value="gpt-6.1-sol"' in page.text
        assert "GPT-6 Astra" in page.text and "$0.40" in page.text
        assert "Точный ID модели OpenAI" not in page.text


def test_legacy_numbers_remain_stable_when_backfilled_and_repeated(database):
    settings, engine, uow, queries, location, service, queue = setup(database)
    with uow() as tx:
        for _ in range(2):
            job_id = str(uuid4())
            assert tx.repo.queue(
                {
                    "id": job_id,
                    "kind": "generate",
                    "location_id": location["id"],
                    "prompt": "legacy prompt",
                }
            )
            tx.repo.change("jobs", job_id, {"status": "failed"})
            tx.repo.audit("temporary_public_fill", job_id, location["id"])
    audit = AIAudit(engine, settings)
    originals = [audit.resolve("ivanovo", n) for n in (1, 2)]
    new_id = service.regenerate(originals[1], model="gpt-5")
    assert [audit.resolve("ivanovo", n) for n in (1, 2)] == originals
    assert audit.resolve("ivanovo", 3) == new_id
    assert audit.detail(originals[0])["job"]["log_path"] == "/logs/ivanovo/1"


def test_concurrent_manual_repeats_get_one_new_number_and_preserve_old_failure(database):
    settings, engine, uow, queries, location, service, queue = setup(database)
    service.enqueue_public_fill(location["id"])
    old = queue.claim()
    queue.fail(old, "Original failure", retry=False)

    def repeat(_):
        try:
            return service.regenerate(old["id"])
        except Conflict:
            return None

    with ThreadPoolExecutor(2) as pool:
        result = list(pool.map(repeat, range(2)))
    assert sum(bool(r) for r in result) == 1
    audit = AIAudit(engine, settings)
    assert audit.resolve("ivanovo", 1) == old["id"]
    new_id = next(r for r in result if r)
    assert audit.resolve("ivanovo", 2) == new_id
    assert audit.detail(old["id"])["job"]["error"] == "Original failure"
    queue.fail(queue.claim(), "Second failure", retry=False)
    service.retry(new_id)
    assert audit.resolve("ivanovo", 3)
    assert queries.get("jobs", new_id)["status"] == "failed"
