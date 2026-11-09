import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
from pydantic import SecretStr

from involta_guide.domain.entities import Conflict
from involta_guide.infrastructure.ai.audit import AIAudit
from involta_guide.infrastructure.ai.openai import OpenAIProvider
from involta_guide.infrastructure.jobs.worker import Worker

from .editor_fixture import enqueue_fixture
from .test_jobs import provider_payload, setup


def test_model_snapshot_changes_generation_only_and_records_actual_model(database):
    settings, engine, uow, queries, location, service, queue = setup(database)
    settings = settings.model_copy(update={"openai_api_key": SecretStr("fake-no-paid-calls")})
    provider = OpenAIProvider(settings, engine)
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "resp_model_snapshot",
                "status": "completed",
                "output": [
                    {"content": [{"type": "output_text", "text": json.dumps(provider_payload())}]}
                ],
            },
        )

    provider.client.close()
    provider.client = httpx.Client(transport=httpx.MockTransport(handler))
    service.provider = provider
    service.save_filling_settings("Первый промпт для будущего наполнения. " * 3, "gpt-5")
    enqueue_fixture(service, location["id"])
    job = queries.rows("jobs")[0]
    service.save_filling_settings("Второй промпт для будущего наполнения. " * 3, "gpt-4.1")
    assert queries.get("jobs", job["id"])["prompt"] == "Первый промпт для будущего наполнения. " * 3
    with engine.begin() as conn:
        conn.execute(__import__("sqlalchemy").text("DELETE FROM audit"))
    try:
        assert Worker(queue, service).once()
        assert queries.get("jobs", job["id"])["status"] == "done"
        assert queries.rows("ai_usage")[0]["model"] == "gpt-5"
        assert [r["model"] for r in captured] == ["gpt-5"]
        assert provider.model == settings.openai_model
    finally:
        provider.close()


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
    enqueue_fixture(service, location["id"])
    old = queue.claim()
    queue.fail(old, "Original failure", retry=False)

    def repeat(_):
        try:
            return service.regenerate(old["id"])
        except Conflict:
            return None

    with ThreadPoolExecutor(2) as pool:
        result = list(pool.map(repeat, range(2)))
    assert len({r for r in result if r}) == 1
    audit = AIAudit(engine, settings)
    assert audit.resolve("ivanovo", 1) == old["id"]
    new_id = next(r for r in result if r)
    assert audit.resolve("ivanovo", 2) == new_id
    assert audit.detail(old["id"])["job"]["error"] == "Original failure"
    queue.fail(queue.claim(), "Second failure", retry=False)
    service.regenerate(new_id)
    assert audit.resolve("ivanovo", 3)
    assert queries.get("jobs", new_id)["status"] == "failed"


def test_reset_deletion_cannot_erase_daily_provider_limits(database):
    import pytest
    from sqlalchemy import text

    from involta_guide.domain.entities import RuleViolation

    settings, engine, uow, queries, city, service, queue = setup(database)
    settings = settings.model_copy(
        update={"openai_api_key": SecretStr("fake-limit-test"), "daily_ai_calls": 1}
    )
    provider = OpenAIProvider(settings, engine)
    calls = []

    def handle(request):
        calls.append(request.method)
        return httpx.Response(
            200,
            json={
                "id": "resp_limit",
                "status": "completed",
                "output": [
                    {"content": [{"type": "output_text", "text": json.dumps(provider_payload())}]}
                ],
            },
        )

    provider.client.close()
    provider.client = httpx.Client(transport=httpx.MockTransport(handle))
    service.provider = provider
    try:
        enqueue_fixture(service, city["id"])
        assert Worker(queue, service).once()
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM generation_requests"))
            conn.execute(text("DELETE FROM ai_usage"))
            conn.execute(text("DELETE FROM audit"))
        other = queries.directory(query="Шуя")["rows"][0]
        enqueue_fixture(service, other["id"])
        job = queries.rows("jobs", {"location_id": other["id"]})[0]
        with pytest.raises(RuleViolation, match="лимит"):
            provider.generate("new request", job["id"])
        assert calls == ["POST"]
    finally:
        provider.close()
