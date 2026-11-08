import httpx
import pytest
from fastapi.testclient import TestClient

from involta_guide.infrastructure.ai.audit import AIAudit
from involta_guide.infrastructure.jobs.worker import Worker
from involta_guide.main import create_app

from .test_background_generation import configured, envelope, task

USAGE = {
    "input_tokens": 12500,
    "output_tokens": 6500,
    "total_tokens": 19000,
    "input_tokens_details": {"cached_tokens": 10000},
    "output_tokens_details": {"reasoning_tokens": 4000},
}


def token_log(database, *, state="completed", usage=USAGE, invalid=False):
    calls = []

    def handler(request):
        calls.append(request.method)
        body = envelope(state)
        body["usage"] = usage
        if state == "incomplete":
            body["incomplete_details"] = {"reason": "max_output_tokens"}
        if invalid:
            body["output"][0]["content"][0]["text"] = '{"not":"a valid generation"}'
        return httpx.Response(200, json=body)

    return configured(database, handler), calls


@pytest.mark.parametrize(
    "state,usage,invalid,total,cached,reasoning",
    [
        ("completed", USAGE, False, 19000, 10000, 4000),
        ("incomplete", USAGE, False, 19000, 10000, 4000),
        ("completed", USAGE, True, 19000, 10000, 4000),
        ("completed", {"input_tokens": 150, "output_tokens": 250}, False, 400, None, None),
        ("completed", None, False, None, None, None),
    ],
)
def test_tokens_survive_errors_export_without_double_counting_and_do_not_trigger_requests(
    database, state, usage, invalid, total, cached, reasoning
):
    context, calls = token_log(database, state=state, usage=usage, invalid=invalid)
    settings, engine, queries, _, service, queue, provider, job = context
    try:
        assert Worker(queue, service).once()
        assert task(engine, job)["response_json"]["usage"] == usage
        audit = AIAudit(engine, settings)
        detail = audit.detail(job["id"])
        counts = detail["diagnostics"]
        assert counts["total_tokens"] == total
        assert counts["cached_input_tokens"] == cached
        assert counts["reasoning_tokens"] == reasoning
        if usage:
            assert counts["input_tokens"] == usage["input_tokens"]
            assert counts["output_tokens"] == usage["output_tokens"]
            saved = queries.rows("ai_usage")
            assert len(saved) == 1
            assert saved[0]["input_tokens"] == usage["input_tokens"]
            assert saved[0]["output_tokens"] == usage["output_tokens"]
        if state == "incomplete" or invalid:
            assert queries.get("jobs", job["id"])["status"] == "failed"
        with TestClient(create_app(settings)) as client:
            path = "/guide" + detail["job"]["log_path"]
            for _ in range(2):
                page = client.get(path)
                assert page.status_code == 200 and "Расход токенов" in page.text
                expected = total if total is not None else "Нет данных"
                assert f'data-token-count="total_tokens">{expected}</dd>' in page.text
                exported = client.get(path + ".json")
                assert exported.status_code == 200
                assert exported.json()["diagnostics"] == counts
        assert calls == ["POST"]
    finally:
        provider.close()
