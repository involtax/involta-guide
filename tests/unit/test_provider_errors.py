import httpx
import pytest

from involta_guide.infrastructure.ai.openai import provider_error


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("insufficient_quota", "баланс API"),
        ("rate_limit_exceeded", "частоты запросов"),
        ("unsupported_country_region_territory", "регион сервера"),
    ],
)
def test_error_distinguishes_provider_reasons_without_echoing_body(code, expected):
    response = httpx.Response(
        429,
        json={"error": {"code": code, "message": "secret prompt sk-test-never-expose"}},
        headers={"x-request-id": "req_test123"},
    )
    message = provider_error(response)
    assert f"[{code}]" in message and expected in message
    assert "req_test123" in message
    assert "secret" not in message and "sk-test" not in message


@pytest.mark.parametrize("body", [b"<html>secret</html>", b"null", b"[]", b'{"error":[]}'])
def test_nonstandard_error_keeps_http_status(body):
    assert provider_error(httpx.Response(403, content=body)) == "OpenAI HTTP 403"


def test_unknown_code_and_untrusted_request_id_are_not_exposed():
    response = httpx.Response(
        429,
        json={"error": {"code": "sk-secret", "type": "insufficient_quota"}},
        headers={"x-request-id": "secret prompt"},
    )
    message = provider_error(response)
    assert "insufficient_quota" in message
    assert "secret" not in message
