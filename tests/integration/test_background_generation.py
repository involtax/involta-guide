import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text

from involta_guide.domain.entities import RuleViolation
from involta_guide.infrastructure.ai.audit import AIAudit
from involta_guide.infrastructure.ai.background import resume_generation
from involta_guide.infrastructure.ai.openai import OpenAIProvider
from involta_guide.infrastructure.jobs.worker import Worker
from involta_guide.infrastructure.storage.audio import FileAudioStorage
from involta_guide.main import create_app

from .test_jobs import FakeAI, setup
from .test_web import login, token


def configured(database, handler):
    settings, engine, uow, queries, location, service, queue = setup(database)
    settings = settings.model_copy(update={"openai_api_key": SecretStr("fake-background-key")})
    provider = OpenAIProvider(
        settings, engine, FileAudioStorage(settings.data_dir, settings.max_audio_bytes)
    )
    provider.client.close()
    provider.client = httpx.Client(transport=httpx.MockTransport(handler))
    service.provider = provider
    queue.settings = settings
    service.enqueue(location["id"])
    job = queries.rows("jobs")[0]
    return settings, engine, queries, location, service, queue, provider, job


def due(engine):
    with engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET available_at=now() WHERE status='queued'"))


def task(engine, job):
    with engine.connect() as conn:
        return dict(
            conn.execute(
                text("SELECT * FROM generation_requests WHERE job_id=:id"), {"id": job["id"]}
            )
            .mappings()
            .one()
        )


def envelope(status="completed", content=None):
    return {
        "id": "resp_background_test",
        "status": status,
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(content or FakeAI().generate("", "")),
                    }
                ],
            }
        ]
        if status == "completed"
        else [],
        "usage": {"input_tokens": 150, "output_tokens": 250} if status == "completed" else None,
    }


def test_restart_and_repeated_poll_failures_never_repeat_post(database):
    calls = []

    def handler(request):
        calls.append(request.method)
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["background"] is True and body["store"] is True
            assert body["tools"] == [{"type": "web_search_preview"}]
            return httpx.Response(200, json=envelope("queued"))
        assert request.url.path.endswith("/resp_background_test")
        if len(calls) < 6:
            raise httpx.ReadTimeout("The read operation timed out", request=request)
        return httpx.Response(200, json=envelope())

    settings, engine, queries, location, service, queue, provider, job = configured(
        database, handler
    )
    try:
        assert Worker(queue, service).once()
        sent_prompt = task(engine, job)["request_json"]["input"][1]["content"]
        # Editing geographical context during polling cannot rewrite the recorded prompt.
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE locations SET coverage_note='Changed while waiting' WHERE id=:id"),
                {"id": location["id"]},
            )
        for _ in range(5):
            due(engine)
            # A fresh worker each time, and no in-memory response ID is required.
            assert Worker(queue, service).once()
        result = queries.get("jobs", job["id"])
        assert result["status"] == "done" and result["attempts"] == 1
        assert calls == ["POST"] + ["GET"] * 5
        assert len(queries.rows("ai_usage")) == 1
        assert len(queries.rows("versions")) == 1
        assert queries.rows("versions")[0]["prompt"] == sent_prompt
        assert queries.get("locations", location["id"])["published_version"] is None
        saved = task(engine, job)
        assert saved["response_json"]["status"] == "completed" and saved["poll_count"] == 5
        assert queries.rows("ai_usage")[0]["input_tokens"] == 150
        assert (
            AIAudit(engine, settings).detail(job["id"])["job"]["stage_label"]
            == "Черновик создан; ещё не опубликован"
        )
    finally:
        provider.close()


def test_expired_worker_lease_resumes_existing_response_after_attempt_limit(database):
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(
            200, json=envelope("in_progress" if request.method == "POST" else "completed")
        )

    settings, engine, queries, location, service, queue, provider, job = configured(
        database, handler
    )
    try:
        Worker(queue, service).once()
        due(engine)
        interrupted = queue.claim()
        assert interrupted["id"] == job["id"]
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE jobs SET attempts=99,lease_until=now()-interval '1 second' WHERE id=:id"
                ),
                {"id": job["id"]},
            )
        assert Worker(queue, service).once()
        assert queries.get("jobs", job["id"])["status"] == "done"
        assert calls == ["POST", "GET"]
        assert not queue.heartbeat(interrupted)
    finally:
        provider.close()


@pytest.mark.parametrize("failure", ["timeout", "server", "malformed"])
def test_uncertain_submission_stops_and_does_not_resubmit(database, failure):
    calls = []

    def handler(request):
        calls.append(request.method)
        if failure == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        return (
            httpx.Response(502, text="upstream error")
            if failure == "server"
            else httpx.Response(200, text="bad json")
        )

    settings, engine, queries, location, service, queue, provider, job = configured(
        database, handler
    )
    try:
        assert Worker(queue, service).once()
        assert queries.get("jobs", job["id"])["status"] == "failed"
        assert task(engine, job)["state"] == "unknown"
        assert not Worker(queue, service).once()
        with pytest.raises(ValueError):
            resume_generation(engine, job["id"], 3600)
        # Even accidental requeuing cannot send a second POST.
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE jobs SET status='queued',available_at=now() WHERE id=:id"),
                {"id": job["id"]},
            )
        Worker(queue, service).once()
        assert calls == ["POST"] and not queries.rows("versions")
    finally:
        provider.close()


def test_interrupted_submission_marker_is_not_sent_again(database):
    calls = []

    def handler(request):
        calls.append(request.method)
        # Simulate SIGKILL after the POST starts, before its ID is persisted.
        raise SystemExit()

    settings, engine, queries, location, service, queue, provider, job = configured(
        database, handler
    )
    try:
        with pytest.raises(SystemExit):
            Worker(queue, service).once()
        assert task(engine, job)["state"] == "submitting"
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE jobs SET lease_until=now()-interval '1 second' WHERE id=:id"),
                {"id": job["id"]},
            )
        Worker(queue, service).once()
        assert calls == ["POST"]
        assert queries.get("jobs", job["id"])["status"] == "failed"
        assert task(engine, job)["state"] == "unknown"
    finally:
        provider.close()


def test_poll_deadline_and_manual_resume_reuse_the_response(database):
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(
            200, json=envelope("queued" if request.method == "POST" else "completed")
        )

    settings, engine, queries, location, service, queue, provider, job = configured(
        database, handler
    )
    try:
        Worker(queue, service).once()
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE generation_requests SET deadline_at=now()-interval '1 second'")
            )
        due(engine)
        Worker(queue, service).once()
        assert queries.get("jobs", job["id"])["status"] == "failed"
        assert task(engine, job)["state"] == "paused"
        resume_generation(engine, job["id"], 3600)
        Worker(queue, service).once()
        assert calls == ["POST", "GET"] and queries.get("jobs", job["id"])["status"] == "done"
    finally:
        provider.close()


def test_saved_response_can_be_revalidated_without_network(database, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json=envelope())

    settings, engine, queries, location, service, queue, provider, job = configured(
        database, handler
    )
    try:
        from involta_guide.application.services import jobs as module

        original = module.rank

        def broken(*args):
            raise RuleViolation("temporary parser failure")

        monkeypatch.setattr(module, "rank", broken)
        Worker(queue, service).once()
        assert task(engine, job)["state"] == "completed"
        assert queries.get("jobs", job["id"])["status"] == "failed"
        with TestClient(create_app(settings)) as client:
            url = "/guide/admin/actions/resume-generation"
            assert (
                client.post(url, data={"log_slug": "ivanovo", "log_number": 1}).status_code == 401
            )
            login(client)
            page = client.get("/guide/logs/ivanovo/1")
            assert "Проверить сохранённый ответ повторно" in page.text
            csrf = token(page.text)
            response = client.post(
                url,
                data={"log_slug": "ivanovo", "log_number": 1, "_csrf": csrf},
                headers={"Origin": "http://testserver"},
            )
            assert response.status_code == 200, response.text
        monkeypatch.setattr(module, "rank", original)
        provider.client.close()  # Successful completion must not even touch a closed client.
        Worker(queue, service).once()
        assert calls == ["POST"]
        assert queries.get("jobs", job["id"])["status"] == "done"
        assert len(queries.rows("versions")) == 1
    finally:
        provider.close()


def test_terminal_incomplete_response_is_kept_and_not_retried(database):
    def handler(request):
        data = envelope("incomplete")
        data.update(
            incomplete_details={"reason": "max_output_tokens"},
            usage={"input_tokens": 150, "output_tokens": 10000},
        )
        return httpx.Response(200, json=data)

    settings, engine, queries, location, service, queue, provider, job = configured(
        database, handler
    )
    try:
        Worker(queue, service).once()
        assert "max_output_tokens" in queries.get("jobs", job["id"])["error"]
        assert task(engine, job)["response_json"]["status"] == "incomplete"
        assert queries.rows("ai_usage")[0]["output_tokens"] == 10000
        assert not Worker(queue, service).once()
    finally:
        provider.close()


def test_background_capacity_is_bounded_across_workers(database):
    settings, engine, uow, queries, location, service, queue = setup(database)
    for city in queries.directory(query="")["rows"][:5]:
        service.enqueue(city["id"])
    with ThreadPoolExecutor(5) as pool:
        claimed = list(pool.map(lambda _: queue.claim(), range(5)))
    assert sum(job is not None for job in claimed) == settings.generation_parallel_limit


def test_rollback_pauses_remote_work_without_deleting_response_identity(database):
    import runpy
    from pathlib import Path

    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(
            200, json=envelope("queued" if request.method == "POST" else "completed")
        )

    settings, engine, queries, location, service, queue, provider, job = configured(
        database, handler
    )
    try:
        Worker(queue, service).once()
        guard = runpy.run_path(
            str(Path(__file__).resolve().parents[2] / "deploy/pause-background.py")
        )
        guard["pause_background"](engine)
        assert queries.get("jobs", job["id"])["status"] == "failed"
        assert task(engine, job)["state"] == "paused"
        assert task(engine, job)["response_id"] == "resp_background_test"
        assert not Worker(queue, service).once()
        resume_generation(engine, job["id"], 3600)
        Worker(queue, service).once()
        assert calls == ["POST", "GET"] and queries.get("jobs", job["id"])["status"] == "done"
    finally:
        provider.close()


def test_editor_change_preserved_but_remote_answer_still_collected(database):
    from involta_guide.application.services.editorial import EditorialService
    from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow

    from .test_editorial import CONTENT

    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(
            200, json=envelope("queued" if request.method == "POST" else "completed")
        )

    settings, engine, queries, location, service, queue, provider, job = configured(
        database, handler
    )
    try:
        Worker(queue, service).once()
        editor_version = EditorialService(lambda: PostgresUow(engine)).save(
            location["id"], CONTENT, None
        )
        due(engine)
        Worker(queue, service).once()
        assert calls == ["POST", "GET"]
        assert task(engine, job)["state"] == "completed"
        assert queries.get("jobs", job["id"])["status"] == "failed"
        assert queries.get("locations", location["id"])["draft_version"] == editor_version
        assert len(queries.rows("versions")) == 1
    finally:
        provider.close()
