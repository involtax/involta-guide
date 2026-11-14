"""Private recovery of cached content responses, with no repeat provider request."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text

from involta_guide.catalogue.service import CityContent
from involta_guide.domain.entities import RuleViolation
from involta_guide.infrastructure.ai.openai import OpenAIProvider
from involta_guide.infrastructure.jobs.worker import Worker
from involta_guide.main import create_app

from .test_jobs import setup
from .test_regional_yandex_discovery import PAYLOAD


@pytest.fixture
def saved_failure(database, monkeypatch):
    settings, engine, uow, queries, city, service, queue = setup(database)
    settings.openai_api_key = SecretStr("mock-only")
    settings.agent_api_token = SecretStr("private-reprocess-test-token-32-characters")
    headers = {"Authorization": "Bearer " + settings.agent_api_token.get_secret_value()}
    calls = []

    def handler(request):
        calls.append(request.method)
        assert calls == ["POST"], "Reprocessing must not contact the provider"
        return httpx.Response(
            200,
            json={
                "id": "resp_cached_discovery",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": PAYLOAD.read_text()}],
                    }
                ],
            },
        )

    provider = OpenAIProvider(settings, engine)
    provider.client.close()
    provider.client = httpx.Client(transport=httpx.MockTransport(handler))
    service.provider = provider
    identity = CityContent(uow).enqueue(city["id"], "discover")

    def fail_application(*args):
        raise RuleViolation("Simulated validation failure after saving the provider response")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(CityContent, "_apply", fail_application)
            assert Worker(queue, service).once()
        assert queries.get("jobs", identity)["status"] == "failed"
        with TestClient(create_app(settings)) as client:
            yield client, headers, identity, city, uow, queries, service, queue, calls
    finally:
        provider.close()


def test_cached_discovery_reprocess_is_authenticated_atomic_and_idempotent(saved_failure):
    client, headers, identity, city, uow, queries, service, queue, calls = saved_failure
    base = "/guide/api/admin/jobs/" + identity
    url = base + "/reprocess"
    status = client.get(base, headers=headers).json()
    assert status["location_id"] == city["id"]
    body = {"request_id": str(uuid4()), "expected_revision": status["location_revision"]}
    assert client.post(url, json=body).status_code == 401
    assert client.post(url, json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post(url, json={"request_id": str(uuid4())}, headers=headers).status_code == 400
    assert (
        client.post(
            url, json={**body, "expected_revision": str(uuid4())}, headers=headers
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/guide/api/admin/jobs/" + str(uuid4()) + "/reprocess", json=body, headers=headers
        ).status_code
        == 404
    )

    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda _: client.post(url, json=body, headers=headers), range(2)))
    assert [r.status_code for r in responses] == [202, 202]
    assert responses[0].json() == responses[1].json() == {"job_id": identity}
    assert len(queries.rows("jobs")) == 1
    assert (
        client.post(url, json={**body, "request_id": str(uuid4())}, headers=headers).status_code
        == 409
    )
    assert (
        client.post(
            url, json={**body, "expected_revision": str(uuid4())}, headers=headers
        ).status_code
        == 409
    )
    assert Worker(queue, service).once()
    assert queries.get("jobs", identity)["applied"] is True
    assert len(queries.rows("restaurants")) == 2
    followups = [j for j in queries.rows("jobs") if j["report"].get("operation") == "research"]
    assert len(followups) == 2 and all(j["status"] == "queued" for j in followups)
    assert calls == ["POST"]
    assert client.post(url, json=body, headers=headers).status_code == 202
    assert len(queries.rows("jobs")) == 3
    # A delayed retry after an HTTP timeout must not overwrite the saved result or queue twice.
    assert client.get(base, headers=headers).json()["applied"] is True


@pytest.mark.parametrize(
    "guard", ["missing", "incomplete", "epoch", "reset", "active", "non_content"]
)
def test_reprocess_rejects_unavailable_or_unsafe_saved_response(saved_failure, guard):
    client, headers, identity, city, uow, queries, _, _, calls = saved_failure
    with uow() as work:
        if guard == "missing":
            work.session.execute(
                text("UPDATE generation_requests SET response_json='{}'::jsonb WHERE job_id=:id"),
                {"id": identity},
            )
        elif guard == "incomplete":
            work.session.execute(
                text("UPDATE generation_requests SET state='paused' WHERE job_id=:id"),
                {"id": identity},
            )
        elif guard == "epoch":
            work.session.execute(
                text("UPDATE locations SET content_epoch=content_epoch+1 WHERE id=:id"),
                {"id": city["id"]},
            )
        elif guard == "reset":
            work.repo.change("locations", city["id"], {"reset_pending": True})
        elif guard == "non_content":
            work.repo.change("jobs", identity, {"report": {}})
    if guard == "active":
        CityContent(uow).enqueue(city["id"], "intro")
    url = "/guide/api/admin/jobs/" + identity + "/reprocess"
    response = client.post(
        url, headers=headers, json={"request_id": str(uuid4()), "expected_revision": None}
    )
    assert response.status_code == (
        404 if guard == "non_content" else 400 if guard in ("missing", "incomplete") else 409
    )
    assert queries.get("jobs", identity)["status"] == "failed"
    assert calls == ["POST"]


def test_repeated_command_after_another_failure_does_not_requeue(saved_failure, monkeypatch):
    client, headers, identity, _, _, queries, service, queue, calls = saved_failure
    url = "/guide/api/admin/jobs/" + identity + "/reprocess"
    body = {"request_id": str(uuid4()), "expected_revision": None}
    assert client.post(url, json=body, headers=headers).status_code == 202

    def fail_application(*args):
        raise RuleViolation("Still invalid")

    monkeypatch.setattr(CityContent, "_apply", fail_application)
    assert Worker(queue, service).once()
    assert queries.get("jobs", identity)["status"] == "failed"
    assert client.post(url, json=body, headers=headers).status_code == 202
    assert queries.get("jobs", identity)["status"] == "failed"
    assert calls == ["POST"]
