from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import text

from involta_guide.application.services.editorial import EditorialService
from involta_guide.application.services.jobs import JobService
from involta_guide.application.services.locations import Queries
from involta_guide.domain.entities import Conflict
from involta_guide.domain.rules import FACTORS
from involta_guide.infrastructure.jobs.queue import JobQueue
from involta_guide.infrastructure.jobs.worker import Worker
from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow
from involta_guide.infrastructure.security import Security
from involta_guide.infrastructure.storage.audio import FileAudioStorage

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
            "warnings": [],
        }

    def transcribe(self, path, mime, job_id):
        return "Добавьте новый ресторан Тест"

    def analyze(self, prompt, job_id, *, alex=False):
        if alex:
            return {
                "actions": [
                    {
                        "kind": "update",
                        "restaurantId": self.restaurant_id,
                        "changes": {"description": "Голосовая правка", "alexStar": True},
                    }
                ]
            }
        return {
            "entries": [
                {
                    "restaurantId": None,
                    "restaurantName": "Новый",
                    "type": "review",
                    "text": "Предлагаю новый ресторан",
                    "quote": "новый ресторан",
                    "sentiment": None,
                    "recommendation": None,
                    "rating": None,
                    "confidence": 0.9,
                    "topics": ["facts"],
                    "experience": "unknown",
                }
            ],
            "suggestions": [
                {
                    "kind": "restaurant",
                    "restaurantId": None,
                    "name": "Новый",
                    "reason": "Пользователь предложил",
                }
            ],
        }


def setup(database, provider=None):
    settings, engine = database

    def uow():
        return PostgresUow(engine)

    queries = Queries(uow)
    location = queries.directory(query="Иваново")["rows"][0]
    storage = FileAudioStorage(settings.data_dir, settings.max_audio_bytes)
    service = JobService(uow, provider or FakeAI(), storage)
    return settings, engine, uow, queries, location, service, JobQueue(engine, settings)


def test_queue_concurrent_claim_lease_fencing_and_recovery(database):
    settings, engine, uow, queries, location, service, queue = setup(database)
    assert service.enqueue(location["id"]) == 1 and service.enqueue(location["id"]) == 0
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
    assert queries.get("locations", location["id"])["published_version"] is None
    assert queries.location(location["id"])["content"]["rankingReport"][0]["totalScore"] == 8


def test_ai_does_not_overwrite_editor_and_missing_key_fails_cleanly(database):
    settings, engine, uow, queries, location, service, queue = setup(database)
    outer = service.provider

    class Racing(FakeAI):
        def generate(self, prompt, job_id):
            EditorialService(uow).save(location["id"], CONTENT, None)
            return outer.generate(prompt, job_id)

    service.provider = Racing()
    service.enqueue(location["id"])
    job = queue.claim()
    with pytest.raises(Conflict):
        service.process(job)
    assert queries.location(location["id"])["content"]["intro"] == "Подборка"
    queue.fail(job, "conflict", retry=False)
    from involta_guide.infrastructure.ai.openai import OpenAIProvider

    provider = OpenAIProvider(
        settings, engine, FileAudioStorage(settings.data_dir, settings.max_audio_bytes)
    )
    service.provider = provider
    service.enqueue(location["id"])
    worker = Worker(queue, service)
    assert worker.once()
    provider.close()
    failed = [r for r in queries.rows("jobs") if r["status"] == "failed"]
    assert any("OPENAI_API_KEY" in row["error"] for row in failed)


def test_review_generates_proposals_without_publication(database):
    settings, engine, uow, queries, location, service, queue = setup(database)
    review_id = str(uuid4())
    with uow() as work:
        work.repo.add(
            "reviews",
            {
                "id": review_id,
                "location_id": location["id"],
                "visitor": "v",
                "audio": "fake.wav",
                "mime": "audio/wav",
                "hash": "h",
                "ip_hash": "ip",
                "device": "test",
            },
        )
        work.repo.queue(
            {
                "id": str(uuid4()),
                "kind": "review",
                "location_id": location["id"],
                "review_id": review_id,
            }
        )
    job = queue.claim()
    service.process(job)
    review = queries.get("reviews", review_id)
    assert review["status"] == "processed" and review["moderation"] == "pending"
    assert len(queries.rows("proposals", {"review_id": review_id})) == 1
    assert queries.get("locations", location["id"])["draft_version"] is None


def test_alex_authority_star_rollback_and_revocation(database):
    settings, engine, uow, queries, location, service, queue = setup(database)
    editor = EditorialService(uow)
    version = editor.save(location["id"], CONTENT, None)
    editor.publish(location["id"], version, None)
    card = queries.location(location["id"])["content"]["restaurants"][0]
    service.provider.restaurant_id = card["id"]
    security = Security(settings, uow)
    token = security.alex_login(settings.alex_secret.get_secret_value())
    session = security.principal(token, alex=True)

    def queue_review():
        review = str(uuid4())
        with uow() as work:
            work.repo.add(
                "reviews",
                {
                    "id": review,
                    "location_id": location["id"],
                    "visitor": "v",
                    "audio": "fake.wav",
                    "mime": "audio/wav",
                    "hash": review,
                    "ip_hash": "ip",
                    "device": "test",
                    "alex_session_id": session["id"],
                },
            )
            work.repo.add(
                "alex_changes",
                {
                    "id": str(uuid4()),
                    "session_id": session["id"],
                    "review_id": review,
                    "location_id": location["id"],
                    "status": "queued",
                    "detail": "queued",
                },
            )
            work.repo.queue(
                {
                    "id": str(uuid4()),
                    "kind": "review",
                    "location_id": location["id"],
                    "review_id": review,
                }
            )
        return queue.claim()

    service.process(queue_review())
    published = queries.public(location["slug"])
    assert published["content"]["restaurants"][0]["alexStar"]
    assert published["content"]["restaurants"][0]["description"] == "Голосовая правка"
    security.revoke(token, alex=True)
    with pytest.raises(ValueError):
        service.process(queue_review())
    manual = queries.location(location["id"])
    manual["content"]["restaurants"][0]["alexStar"] = False
    editor.save(location["id"], manual["content"], manual["location"]["draft_version"])
    assert queries.get("restaurants", card["id"])["alex_star"]


def test_bounded_retries_and_owner_guard(database):
    from involta_guide.infrastructure.ai.openai import ProviderUnavailable

    class Failing(FakeAI):
        def generate(self, prompt, job_id):
            raise ProviderUnavailable("OpenAI HTTP 429")

    settings, engine, uow, queries, location, service, queue = setup(database, Failing())
    service.enqueue(location["id"])
    worker = Worker(queue, service)
    for _ in range(settings.max_job_attempts):
        assert worker.once()
        with engine.begin() as conn:
            conn.execute(text("UPDATE jobs SET available_at=now() WHERE status='queued'"))
    assert not worker.once()
    row = queries.rows("jobs")[0]
    assert row["status"] == "failed" and row["attempts"] == settings.max_job_attempts
    assert row["error"] == "OpenAI HTTP 429"


def test_httpx_adapter_usage_search_and_budget_without_paid_calls(database):
    import httpx
    from pydantic import SecretStr

    from involta_guide.infrastructure.ai.openai import OpenAIProvider

    settings, engine, uow, queries, location, service, queue = setup(database)
    settings = settings.model_copy(
        update={
            "openai_api_key": SecretStr("fake-test-only"),
            "daily_ai_calls": 1,
            "ai_prices_json": '{"gpt-4.1":{"input":1,"output":2}}',
        }
    )
    service.enqueue(location["id"])
    job = queue.claim()
    provider = OpenAIProvider(
        settings, engine, FileAudioStorage(settings.data_dir, settings.max_audio_bytes)
    )
    captured = []

    def handler(request):
        import json

        body = json.loads(request.content)
        captured.append(body)
        assert body["tools"] == [{"type": "web_search_preview"}]
        return httpx.Response(
            200,
            json={
                "usage": {"input_tokens": 100, "output_tokens": 50},
                "output": [{"content": [{"type": "output_text", "text": '{"ok":true}'}]}],
            },
        )

    provider.client.close()
    provider.client = httpx.Client(transport=httpx.MockTransport(handler))
    assert provider.generate("test prompt", job["id"]) == {"ok": True}
    with pytest.raises(ValueError):
        provider.generate("another", job["id"])
    usage = queries.rows("ai_usage")[0]
    assert usage["input_tokens"] == 100 and usage["output_tokens"] == 50
    assert usage["estimated_cost_usd"] == 0.0002
    assert len(captured) == 1
    provider.close()
