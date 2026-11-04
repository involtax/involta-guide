from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

from involta_guide.application.services.jobs import JobService
from involta_guide.application.services.locations import Queries
from involta_guide.domain.entities import Conflict
from involta_guide.domain.rules import FACTORS
from involta_guide.infrastructure.jobs.queue import JobQueue
from involta_guide.infrastructure.jobs.worker import Worker
from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow

from .editor_fixture import FixtureEditor as EditorialService
from .editor_fixture import enqueue_fixture
from .test_editorial import CARD, CONTENT


class FakeAI:
    model = "fake-no-paid-requests"

    def generate(self, prompt, job_id):
        return {
            "intro": "Сгенерировано",
            "restaurants": [CARD],
            "rankingReport": [
                {
                    "name": CARD["name"],
                    "mapsUrl": CARD["mapsUrl"],
                    "scores": dict.fromkeys(FACTORS, 8),
                    "evidence": [
                        {
                            "factor": f,
                            "reason": "test evidence",
                            "confidence": "medium",
                            "sources": [],
                        }
                        for f in FACTORS
                    ],
                    "totalScore": 0,
                }
            ],
            "candidateReview": [
                {
                    "name": CARD["name"],
                    "mapsUrl": CARD["mapsUrl"],
                    "status": "verified",
                    "reason": "Подтверждены карточка и восемь факторов",
                    "sources": [],
                }
            ],
            "warnings": [],
        }


def provider_payload(content=None):
    content = content or FakeAI().generate("", "")
    by_name = {r["name"]: r for r in content.get("rankingReport", [])}
    candidates = []
    for card in content["restaurants"]:
        ranking = by_name[card["name"]]
        sources = list(
            dict.fromkeys(
                [
                    *card.get("sources", []),
                    *[url for e in ranking["evidence"] for url in e.get("sources", [])],
                ]
            )
        )
        # Provider fixtures model evidenced ratings, without performing any real web search.
        sources = sources or ["https://example.com/fixture-evidence"]
        candidates.append(
            {
                **{
                    k: v
                    for k, v in card.items()
                    if k
                    in {
                        "name",
                        "cuisine",
                        "description",
                        "mapsUrl",
                        "phone",
                        "openingHours",
                        "latitude",
                        "longitude",
                    }
                },
                "status": "verified",
                "reason": "Подтверждены карточка и восемь факторов",
                "sources": sources,
                "scores": ranking["scores"],
                "evidence": [
                    {
                        "factor": e["factor"],
                        "reason": e["reason"],
                        "confidence": e["confidence"],
                        "sourceRefs": [sources.index(url) + 1 for url in e.get("sources", [])]
                        or [1],
                    }
                    for e in ranking["evidence"]
                ],
            }
        )
    return {
        "intro": content["intro"],
        "candidates": candidates,
        "warnings": content.get("warnings", []),
    }


def setup(database, provider=None):
    settings, engine = database

    def uow():
        return PostgresUow(engine)

    queries = Queries(uow)
    location = queries.directory(query="Иваново")["rows"][0]
    service = JobService(uow, provider or FakeAI())
    return settings, engine, uow, queries, location, service, JobQueue(engine, settings)


def test_queue_concurrent_claim_lease_fencing_and_recovery(database):
    settings, engine, uow, queries, location, service, queue = setup(database)
    assert (
        enqueue_fixture(service, location["id"]) == 1
        and enqueue_fixture(service, location["id"]) == 0
    )
    with ThreadPoolExecutor(2) as pool:
        jobs = list(pool.map(lambda _: queue.claim(), range(2)))
    assert sum(j is not None for j in jobs) == 1
    old = next(j for j in jobs if j)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE jobs SET lease_until=now()-interval '1 second' WHERE id=:id"),
            {"id": old["id"]},
        )
    assert not queue.heartbeat(old)
    new = queue.claim()
    assert new["id"] == old["id"] and new["lease_owner"] != old["lease_owner"]
    with pytest.raises(Conflict):
        service.process(old)
    service.process(new)
    assert queries.get("jobs", old["id"])["status"] == "done"
    stored = queries.get("locations", location["id"])
    assert stored["revision"] == stored["revision"]
    assert queries.location(location["id"])["content"]["rankingReport"][0]["totalScore"] == 8


def test_generation_saves_candidate_with_null_maps_url(database):
    class NullableCandidateAI(FakeAI):
        def generate(self, prompt, job_id):
            content = super().generate(prompt, job_id)
            content["rankingReport"].append(
                {
                    "name": "Неподтверждённый кандидат",
                    "mapsUrl": None,
                    "scores": dict.fromkeys(FACTORS),
                    "evidence": [{"factor": factor} for factor in FACTORS],
                }
            )
            return content

    _, _, _, queries, location, service, queue = setup(database, NullableCandidateAI())
    enqueue_fixture(service, location["id"])
    job = queue.claim()
    service.process(job)
    assert queries.get("jobs", job["id"])["status"] == "done"
    content = queries.location(location["id"])["content"]
    assert len(content["restaurants"]) == 1
    assert len(content["rankingReport"]) == 1
    historical_report = queries.get("jobs", job["id"])["report"]["rankingReport"]
    assert len(historical_report) == 2
    assert historical_report[1]["mapsUrl"] is None


def test_ai_does_not_overwrite_editor_and_missing_key_fails_cleanly(database):
    settings, engine, uow, queries, location, service, queue = setup(database)
    outer = service.provider

    class Racing(FakeAI):
        def generate(self, prompt, job_id):
            EditorialService(uow).save(location["id"], CONTENT, None)
            return outer.generate(prompt, job_id)

    service.provider = Racing()
    enqueue_fixture(service, location["id"])
    job = queue.claim()
    with pytest.raises(Conflict):
        service.process(job)
    assert queries.location(location["id"])["content"]["intro"] == "Подборка"
    queue.fail(job, "conflict", retry=False)
    from involta_guide.infrastructure.ai.openai import OpenAIProvider

    provider = OpenAIProvider(settings, engine)
    service.provider = provider
    other = queries.directory(query="Шуя")["rows"][0]
    enqueue_fixture(service, other["id"])
    worker = Worker(queue, service)
    assert worker.once()
    provider.close()
    failed = [r for r in queries.rows("jobs") if r["status"] == "failed"]
    assert any("OPENAI_API_KEY" in row["error"] for row in failed)


def test_bounded_retries_and_owner_guard(database):
    from involta_guide.infrastructure.ai.errors import ProviderUnavailable

    class Failing(FakeAI):
        def generate(self, prompt, job_id):
            raise ProviderUnavailable("OpenAI HTTP 429")

    settings, engine, uow, queries, location, service, queue = setup(database, Failing())
    enqueue_fixture(service, location["id"])
    worker = Worker(queue, service)
    assert worker.once()
    assert not worker.once()
    row = queries.rows("jobs")[0]
    assert row["status"] == "failed" and row["attempts"] == 1
    assert row["error"] == "OpenAI HTTP 429"


def test_httpx_adapter_usage_search_and_call_limit_without_paid_calls(database):
    import httpx
    from pydantic import SecretStr

    from involta_guide.infrastructure.ai.openai import OpenAIProvider

    settings, engine, uow, queries, location, service, queue = setup(database)
    settings = settings.model_copy(
        update={
            "openai_api_key": SecretStr("fake-test-only"),
            "daily_ai_calls": 1,
        }
    )
    service.set_generation_model("gpt-4.1")
    enqueue_fixture(service, location["id"])
    job = queue.claim()
    provider = OpenAIProvider(settings, engine)
    captured = []

    def handler(request):
        import json

        body = json.loads(request.content)
        captured.append(body)
        assert body["tools"] == [{"type": "web_search_preview"}]
        return httpx.Response(
            200,
            json={
                "id": "resp_call_limit",
                "status": "completed",
                "usage": {"input_tokens": 100, "output_tokens": 50},
                "output": [
                    {"content": [{"type": "output_text", "text": json.dumps(provider_payload())}]}
                ],
            },
        )

    provider.client.close()
    provider.client = httpx.Client(transport=httpx.MockTransport(handler))
    first = provider.generate("test prompt", job["id"])
    assert first["intro"] == "Сгенерировано"
    assert provider.generate("test prompt", job["id"]) == first
    other = queries.directory(query="Шуя")["rows"][0]
    enqueue_fixture(service, other["id"])
    other_job = next(j for j in queries.rows("jobs") if j["location_id"] == other["id"])
    with pytest.raises(ValueError):
        provider.generate("another", other_job["id"])
    usage = queries.rows("ai_usage")[0]
    assert usage["input_tokens"] == 100 and usage["output_tokens"] == 50
    assert "estimated_cost_usd" not in usage
    assert len(captured) == 1
    provider.close()


@pytest.mark.parametrize("code", ["insufficient_quota", "rate_limit_exceeded"])
def test_provider_429_reason_persisted_without_response_secrets(database, code):
    import httpx
    from pydantic import SecretStr

    from involta_guide.infrastructure.ai.errors import ProviderUnavailable
    from involta_guide.infrastructure.ai.openai import OpenAIProvider

    settings, engine, uow, queries, location, service, queue = setup(database)
    settings = settings.model_copy(update={"openai_api_key": SecretStr("fake-test-only")})
    enqueue_fixture(service, location["id"])
    job = queue.claim()
    provider = OpenAIProvider(settings, engine)
    provider.client.close()
    provider.client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                429,
                json={"error": {"code": code, "message": "sk-private-do-not-expose"}},
                headers={"x-request-id": "req_test123"},
            )
        )
    )
    try:
        with pytest.raises(ProviderUnavailable, match=code) as failure:
            provider.generate("test prompt", job["id"])
        usage = queries.rows("ai_usage")[0]
        assert usage["status"] == "failed"
        assert usage["detail"] == str(failure.value)
        assert "req_test123" in usage["detail"]
        assert "sk-private" not in usage["detail"]
        detail = provider.audit.detail(job["id"])
        responses = [
            e["payload"] for e in detail["events"] if e["payload"].get("event") == "response"
        ]
        assert responses[0]["data"]["status"] == 429
        assert code in responses[0]["data"]["body"]
        assert "sk-private" not in responses[0]["data"]["body"]
        assert not [r for r in queries.rows("reviews") + queries.rows("jobs") if r["changes"]]
    finally:
        provider.close()
