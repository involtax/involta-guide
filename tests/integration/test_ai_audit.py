import json

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from involta_guide.infrastructure.ai.audit import AIAudit
from involta_guide.infrastructure.ai.openai import OpenAIProvider
from involta_guide.infrastructure.jobs.worker import Worker
from involta_guide.main import create_app

from .test_jobs import FakeAI, setup
from .test_web import login


def test_full_response_survives_validation_failure_and_is_admin_only(database):
    settings, engine, uow, queries, location, service, queue = setup(database)
    settings = settings.model_copy(update={"openai_api_key": SecretStr("sk-private-never-store")})
    queue.settings = settings
    provider = OpenAIProvider(settings, engine)
    response = FakeAI().generate("", "")
    response["restaurants"][0] = {**response["restaurants"][0], "phone": "wrong phone"}
    response["intro"] = "<script>alert('xss')</script> sk-private-never-store"
    envelope = {
        "id": "resp_test_full_response",
        "status": "completed",
        "usage": {"input_tokens": 125, "output_tokens": 256},
        "output": [{"content": [{"type": "output_text", "text": json.dumps(response)}]}],
    }
    provider.client.close()
    provider.client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=envelope,
                headers={"x-request-id": "req_audit123", "set-cookie": "private-cookie"},
            )
        )
    )
    service.provider = provider
    service.enqueue_public_fill(location["id"])
    try:
        assert Worker(queue, service).once()
    finally:
        provider.close()
    job = queries.rows("jobs")[0]
    assert job["status"] == "failed" and job["error"] == "AI response validation failed"
    store = AIAudit(engine, settings)
    detail = store.detail(job["id"])
    assert detail["trace_available"]
    events = [e["payload"] for e in detail["events"]]
    assert {e["event"] for e in events[:2]} == {"job_started", "request"}
    assert [e["event"] for e in events] == [
        "job_started",
        "request",
        "response",
        "generation_status",
        "job_failed",
    ]
    request = next(e for e in events if e["event"] == "request")
    assert request["data"]["json"]["tools"] == [{"type": "web_search_preview"}]
    assert "Иваново" in request["data"]["json"]["input"][1]["content"]
    raw = next(e for e in events if e["event"] == "response")
    assert raw["data"]["status"] == 200
    assert "resp_test_full_response" in raw["data"]["body"]
    assert raw["data"]["headers"]["x-request-id"] == "req_audit123"
    failure = next(e for e in events if e["event"] == "job_failed")
    assert failure["data"]["validation_errors"][0]["loc"] == ["restaurants", 0, "phone"]
    assert "wrong phone" in failure["data"]["traceback"]
    assert detail["calls"][0]["input_tokens"] == 125
    assert not [r for r in queries.rows("reviews") + queries.rows("jobs") if r["changes"]]
    assert "sk-private" not in json.dumps(detail)
    assert "private-cookie" not in json.dumps(detail)

    with TestClient(create_app(settings)) as client:
        url = "/guide/logs/ivanovo/1.json?download=1"
        assert client.get("/guide/admin/ai-audit/" + job["id"] + ".json").status_code == 404
        assert "resp_test_full_response" not in client.get("/guide/admin?tab=ai-audit").text
        login(client)
        page = client.get("/guide/logs/ivanovo/1")
        assert page.status_code == 200, page.text
        assert "wrong phone" in page.text and "req_audit123" in page.text
        assert "<script>alert" not in page.text
        assert "no-store" in page.headers["cache-control"]
        exported = client.get(url)
        assert exported.status_code == 200 and exported.json()["job"]["id"] == job["id"]
        assert "attachment" in exported.headers["content-disposition"]
        assert client.get("/guide/logs?q=Иваново&status=failed").status_code == 200


def test_audit_preserves_each_attempt_success_and_legacy_data(database):
    settings, engine, uow, queries, location, service, queue = setup(database)
    service.enqueue_public_fill(location["id"])
    job = queries.rows("jobs")[0]
    audit = AIAudit(engine, settings)
    assert not audit.detail(job["id"])["trace_available"]
    assert Worker(queue, service).once()
    detail = audit.detail(job["id"])
    assert detail["job"]["status"] == "done"
    assert detail["changes"][0]["diff"]
    assert "versions" not in detail
    audit.record(job["id"], "job_started", {"attempt": 2})
    assert len(audit.detail(job["id"])["events"]) == len(detail["events"]) + 1
    assert audit.listing(999, "Иваново")["total"] == 1
    assert audit.listing(1, "no-matching-location")["total"] == 0
