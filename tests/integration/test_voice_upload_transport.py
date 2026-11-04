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


def test_real_http_rejects_large_invalid_audio_promptly(database, voice_database):
    settings, _, _, queries, city, _, _ = setup(database)
    vs, ve = voice_database
    with serving(voice_app(vs)) as voice_url:
        settings = settings.model_copy(
            update={
                "voice_url": voice_url,
                "voice_service_secret": vs.service_secret,
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
            assert client.post("/guide/voice/api/v1/identity?location=ivanovo").status_code == 200
            for size in (1024, 65536, 150000, 524288, 1200000):
                response = client.post(
                    "/guide/voice/api/v1/submissions",
                    params={
                        "location": city["slug"],
                        "content_epoch": 1,
                        "idempotency_key": str(uuid4()),
                    },
                    files={"audio": ("invalid.webm", b"x" * size, "audio/webm")},
                )
                assert response.status_code == 400, response.text
                assert "application/json" in response.headers["content-type"]
    assert queries.rows("reviews") == []
    with ve.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM submissions")).scalar() == 0


def test_all_roles_share_normal_flow_retries_audio_and_private_debug(database, voice_database):
    from fastapi.testclient import TestClient

    from .test_voice_guide import client_for
    from .test_voice_service import recording
    from .test_web import login

    settings, engine, _, queries, city, _, _ = setup(database)
    vs, ve = voice_database
    settings = settings.model_copy(
        update={"voice_service_secret": vs.service_secret, "voice_enabled": True}
    )
    app = guide_app(settings)
    app.state.voice_client = client_for(settings, vs)
    with TestClient(app) as client:
        headers = {
            "origin": "http://testserver",
            "user-agent": "Full raw beta UA",
            "role": "admin",
            "x-forwarded-for": "forged",
        }
        identity = client.post(
            "/guide/voice/api/v1/identity?location=ivanovo", headers=headers
        ).json()
        assert identity == {"ready": True, "content_epoch": 1}
        owner = client.cookies.get("guide_visitor")
        params = {"location": city["slug"], "content_epoch": 1, "idempotency_key": str(uuid4())}

        def upload():
            return client.post(
                "/guide/voice/api/v1/submissions",
                params=params,
                content=recording(),
                headers={**headers, "content-type": "audio/wav"},
            )

        first = upload()
        assert first.status_code == 202, first.text
        assert upload().json()["reference"] == first.json()["reference"]
        login(client)
        assert (
            client.post("/guide/voice/api/v1/identity?location=ivanovo", headers=headers).json()
            == identity
        )
        trace = str(uuid4())
        headers["x-voice-request-id"] = trace
        params["idempotency_key"] = str(uuid4())
        second = upload()
        assert second.status_code == 202, second.text
        assert upload().json()["reference"] == second.json()["reference"]
        assert queries.rows("reviews") == [] and queries.rows("jobs") == []
        from involta_guide.infrastructure.voice.consumer import Consumer

        Consumer(engine, app.state.voice_client).once()
        listing = client.get("/guide/voice/api/v1/reviews?location=ivanovo").json()
        assert listing["content_epoch"] == 1
        history = listing["rows"]
        assert {r["reference"] for r in history} == {
            first.json()["reference"],
            second.json()["reference"],
        }
        events = client.get("/guide/voice/api/v1/debug/" + trace).json()["events"]
        assert events[-1]["received_bytes"] == len(recording()) and events[-1]["status"] == 202
        assert owner not in str(events)
        debug_path = "/guide/voice/api/v1/debug/" + trace + "?location=ivanovo&content_epoch=1"
        assert (
            client.post(
                debug_path,
                headers=headers,
                json={
                    "version": 3,
                    "recording": {
                        "bytes": len(recording()),
                        "durationMs": 6000,
                        "mime": "audio/wav",
                        "content_epoch": 1,
                    },
                    "timeline": [{"name": "stop_requested", "reason": "silence"}],
                    "vad": {"state": "ready"},
                    "events": [
                        {
                            "response_body": "<html>proxy full response</html>",
                            "csrf_token": "test-private-token",
                        }
                    ],
                },
            ).status_code
            == 200
        )
        browser_report = client.get(debug_path).json()
        assert "proxy full response" in str(browser_report)
        assert "test-private-token" not in str(browser_report)
        event = browser_report["events"][-1]
        assert event["report"]["timeline"][0]["reason"] == "silence"
        assert event["report"]["vad"]["state"] == "ready"
        assert event["report"]["recording"]["durationMs"] == 6000
        public_log = client.get(f"/guide/logs/ivanovo/uploads/{trace}.json").json()
        assert public_log["facts"]["duration_ms"] == 6000
        assert public_log["facts"]["bytes"] == len(recording())
        assert upload().json()["reference"] == second.json()["reference"]
        with ve.connect() as conn:
            values = list(conn.execute(text("SELECT metadata FROM submissions")).scalars())
            assert len(values) == 2
            assert all(
                v["visitor"] == owner and v["user_agent"] == "Full raw beta UA" and "actor" not in v
                for v in values
            )
            assert all(v["ip"] != "forged" and "ip_fingerprint" not in v for v in values)
        client.cookies.clear()
        assert client.get("/guide/voice/api/v1/debug/" + trace).json()["events"] == []
        client.post("/guide/voice/api/v1/identity?location=ivanovo", headers=headers)
        assert (
            client.get("/guide/voice/api/v1/submissions/" + second.json()["reference"]).status_code
            == 404
        )
