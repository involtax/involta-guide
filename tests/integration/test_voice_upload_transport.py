"""Actual HTTP streaming regression: multipart boundaries must survive real Uvicorn flow control."""

import socket
import threading
import time
from contextlib import contextmanager
from uuid import uuid4

import httpx
import uvicorn
from sqlalchemy import text

from involta_guide.main import create_app as guide_app
from involta_voice.main import create_app as voice_app

from .test_jobs import setup
from .test_voice_service import voice_database as voice_database


@contextmanager
def serving(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    worker = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    worker.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.02)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        worker.join(timeout=5)
        sock.close()


def test_real_http_rejects_large_invalid_audio_promptly_for_both_routes(database, voice_database):
    settings, engine, _, queries, city, _, _ = setup(database)
    voice_settings, voice_engine = voice_database
    with serving(voice_app(voice_settings)) as voice_url:
        settings = settings.model_copy(
            update={
                "voice_url": voice_url,
                "voice_service_secret": voice_settings.service_secret,
                "voice_enabled": True,
            }
        )
        with (
            serving(guide_app(settings)) as guide_url,
            httpx.Client(
                base_url=guide_url,
                trust_env=False,
                timeout=5,
                headers={"origin": "http://testserver"},
            ) as client,
        ):
            assert client.post("/guide/voice/api/v1/identity").status_code == 200
            for route in ["/guide/api/reviews", "/guide/voice/api/v1/submissions"]:
                for size in (1024, 65536, 150000, 524288, 1200000):
                    response = client.post(
                        route,
                        data={
                            "locationId": str(city["id"]),
                            "location": city["slug"],
                            "idempotency_key": str(uuid4()),
                        },
                        files={"audio": ("invalid.webm", b"x" * size, "audio/webm")},
                    )
                    assert response.status_code == 400, (
                        route,
                        size,
                        response.status_code,
                        response.text[:200],
                    )
                    assert "application/json" in response.headers["content-type"]
    assert queries.rows("reviews") == []
    with voice_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM submissions")).scalar_one() == 0


def test_one_gateway_preserves_roles_retries_audio_and_private_debug(database, voice_database):
    from fastapi.testclient import TestClient

    from .test_voice_guide import client_for
    from .test_voice_service import recording
    from .test_web import login

    settings, engine, _, queries, city, _, _ = setup(database)
    voice_settings, voice_engine = voice_database
    settings = settings.model_copy(
        update={"voice_service_secret": voice_settings.service_secret, "voice_enabled": True}
    )
    app = guide_app(settings)
    app.state.voice_client = client_for(settings, voice_settings)
    with TestClient(app) as client:
        headers = {"origin": "http://testserver"}
        assert (
            client.post("/guide/voice/api/v1/identity", headers=headers).json()["actor"]
            == "visitor"
        )
        identity_cookie = client.cookies.get("guide_visitor")
        visitor_key = str(uuid4())
        params = {"location": city["slug"], "idempotency_key": visitor_key}

        def upload():
            return client.post(
                "/guide/voice/api/v1/submissions",
                params=params,
                content=recording(),
                headers={**headers, "content-type": "audio/wav"},
            )

        visitor = upload()
        assert visitor.status_code == 202, visitor.text
        assert upload().json()["reference"] == visitor.json()["reference"]
        assert queries.rows("reviews") == []
        login(client)
        trace_id = str(uuid4())
        identity = client.post(
            "/guide/voice/api/v1/identity", headers={**headers, "x-voice-request-id": trace_id}
        ).json()
        assert identity["actor"] == "admin"
        params["idempotency_key"] = str(uuid4())
        assert upload().status_code == 403
        headers["x-csrf-token"] = identity["csrf_token"]
        headers["x-voice-request-id"] = trace_id
        result = upload()
        assert result.status_code == 202, result.text
        reference = result.json()["reference"]
        assert upload().json()["reference"] == reference
        assert len(queries.rows("reviews")) == 1
        assert len(queries.rows("jobs", {"review_id": reference})) == 1
        assert queries.get("reviews", reference)["admin_session_id"]
        assert (
            client.get("/guide/voice/api/v1/submissions/" + reference).json()["state"] == "accepted"
        )
        history = client.get(
            "/guide/voice/api/v1/submissions/mine", params={"location": city["slug"]}
        ).json()["rows"]
        assert {r["reference"] for r in history} == {reference, visitor.json()["reference"]}
        events = client.get("/guide/voice/api/v1/debug/" + trace_id).json()["events"]
        assert events[-1]["received_bytes"] == len(recording())
        assert events[-1]["status"] == 202
        assert identity["csrf_token"] not in str(events)
        assert identity_cookie not in str(events)
        assert settings.session_secret.get_secret_value() not in str(events)
        conflict = client.post(
            "/guide/voice/api/v1/submissions",
            params=params,
            content=recording() + b"changed",
            headers={**headers, "content-type": "audio/wav"},
        )
        assert conflict.status_code == 409
        browser_report = client.post(
            "/guide/voice/api/v1/debug/" + trace_id,
            headers=headers,
            json={
                "timeline": [{"name": "stop_requested", "reason": "silence"}],
                "vad": {"state": "ready"},
                "events": [
                    {
                        "status": 502,
                        "response_body": "<html>full proxy response</html>",
                        "csrf_token": identity["csrf_token"],
                    }
                ],
            },
        )
        assert browser_report.json()["saved"]
        events = client.get("/guide/voice/api/v1/debug/" + trace_id).json()["events"]
        assert events[-1]["stage"] == "browser"
        assert events[-1]["report"]["timeline"][0]["reason"] == "silence"
        assert events[-1]["report"]["vad"]["state"] == "ready"
        assert "full proxy response" in str(events[-1])
        assert identity["csrf_token"] not in str(events[-1])
        page = client.get("/guide/" + city["slug"])
        assert page.text.count('id="guide-voice"') == 1
        assert page.text.count('data-target="guide-voice"') == 1
        assert "/widget.js" not in page.text
        client.cookies.clear()
        assert client.get("/guide/voice/api/v1/debug/" + trace_id).json()["events"] == []
        client.post("/guide/voice/api/v1/identity", headers={"origin": "http://testserver"})
        client.post(
            "/guide/voice/api/v1/debug/" + trace_id,
            headers={"origin": "http://testserver"},
            json={"events": [{"status": 999}]},
        )
        isolated = client.get("/guide/voice/api/v1/debug/" + trace_id).json()["events"]
        assert len(isolated) == 1 and isolated[0]["report"]["events"][0]["status"] == 999
        assert client.get("/guide/voice/api/v1/submissions/" + reference).status_code == 404
    with voice_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM submissions")).scalar_one() == 1


def test_concurrent_admin_retries_enqueue_one_command(database, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from involta_guide.infrastructure.security import Security

    from .test_voice_service import recording

    settings, _, uow, queries, city, _, _ = setup(database)
    app = guide_app(settings)
    reviews = app.state.context.reviews
    session = Security(settings, uow).login("test-password-123456", "test")
    principal = Security(settings, uow).principal(session)
    key = str(uuid4())
    barrier = Barrier(2)
    original = reviews.storage.save

    def save(*args):
        barrier.wait(timeout=10)
        return original(*args)

    monkeypatch.setattr(reviews.storage, "save", save)

    def send(_):
        return reviews.submit(
            city["id"],
            "same-owner",
            "same-ip",
            "browser",
            recording(),
            "audio/wav",
            admin_session=principal["id"],
            idempotency_key=key,
        )

    with ThreadPoolExecutor(2) as workers:
        results = list(workers.map(send, range(2)))
    assert results[0] == results[1]
    assert (
        len(queries.rows("reviews"))
        == len(queries.rows("jobs"))
        == len(queries.rows("alex_changes"))
        == 1
    )
    assert reviews.audio(results[0])[0] == recording()
