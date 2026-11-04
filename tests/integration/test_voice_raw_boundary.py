"""Real raw uploads matching the failed 20-second recording, without ASR calls."""

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from involta_guide.infrastructure.voice.upload_debug import UploadDebug
from involta_guide.main import create_app as guide_app
from involta_voice.main import create_app as voice_app

from .test_jobs import setup
from .test_voice_service import recording
from .test_voice_service import voice_database as voice_database
from .test_voice_upload_transport import serving


@pytest.mark.parametrize("chunked", [False, True])
@pytest.mark.parametrize("administrator", [False, True])
def test_twenty_second_raw_upload_gets_durable_acceptance(
    database, voice_database, chunked, administrator
):
    settings, engine, _, _, city, _, _ = setup(database)
    vs, ve = voice_database
    audio = recording(20.28)
    assert len(audio) > 256 * 1024
    with serving(voice_app(vs)) as voice_url:
        settings = settings.model_copy(
            update={
                "voice_url": voice_url,
                "voice_service_secret": vs.service_secret,
                "voice_enabled": True,
            }
        )
        app = guide_app(settings)
        with (
            serving(app) as guide_url,
            httpx.Client(
                base_url=guide_url,
                trust_env=False,
                timeout=5,
                headers={"origin": "http://testserver"},
            ) as client,
        ):
            if administrator:
                token = app.state.context.security.login("test-password-123456", "test-ip")
                client.cookies.set("guide_admin", token, path="/guide")
            identity = client.post("/guide/voice/api/v1/identity?location=ivanovo")
            assert identity.status_code == 200
            key = str(uuid4())
            request_id = str(uuid4())
            response = client.post(
                "/guide/voice/api/v1/submissions",
                params={"location": city["slug"], "content_epoch": 1, "idempotency_key": key},
                headers={"content-type": "audio/wav", "x-voice-request-id": request_id},
                content=(audio[i : i + 8192] for i in range(0, len(audio), 8192))
                if chunked
                else audio,
            )
            assert response.status_code == 202, response.text
    events = UploadDebug(engine, settings).read(request_id, "", admin=True)
    stages = [e["stage"] for e in events]
    expected = ["received", "body_received", "forwarding", "upstream_response", "response"]
    assert [stage for stage in stages if stage != "body_progress"] == expected
    assert next(e for e in events if e["stage"] == "body_received")["received_bytes"] == len(audio)
    with ve.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM submissions")).scalar_one() == 1


def test_partial_upload_is_observable_before_request_finishes(database, voice_database):
    settings, engine, _, _, city, _, _ = setup(database)
    vs, _ = voice_database
    audio = recording(20.28)
    sent, resume = Event(), Event()
    request_id = str(uuid4())

    def content():
        yield audio[:65536]
        sent.set()
        assert resume.wait(5), "test must release the paused upload"
        yield audio[65536:]

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
                timeout=8,
                headers={"origin": "http://testserver"},
            ) as client,
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            assert client.post("/guide/voice/api/v1/identity?location=ivanovo").status_code == 200
            future = pool.submit(
                client.post,
                "/guide/voice/api/v1/submissions",
                params={
                    "location": city["slug"],
                    "content_epoch": 1,
                    "idempotency_key": str(uuid4()),
                },
                headers={"content-type": "audio/wav", "x-voice-request-id": request_id},
                content=content(),
            )
            try:
                assert sent.wait(2)
                events = []
                for _ in range(100):
                    events = UploadDebug(engine, settings).read(request_id, "", admin=True)
                    if any(e["stage"] == "body_progress" for e in events):
                        break
                    time.sleep(0.01)
                assert not future.done()
                assert any(0 < e.get("received_bytes", 0) <= 65536 for e in events)
                assert not any(e["stage"] in ("body_received", "response") for e in events)
            finally:
                resume.set()
            assert future.result(timeout=5).status_code == 202
