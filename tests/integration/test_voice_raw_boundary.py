"""Real raw-browser uploads around the observed 256 KiB boundary, without ASR calls."""

from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from involta_guide.main import create_app as guide_app
from involta_voice.main import create_app as voice_app

from .test_jobs import setup
from .test_voice_service import recording
from .test_voice_service import voice_database as voice_database
from .test_voice_upload_transport import serving


@pytest.mark.parametrize("chunked", [False, True])
def test_twenty_second_raw_upload_gets_durable_acceptance(database, voice_database, chunked):
    settings, _, _, _, city, _, _ = setup(database)
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
        with (
            serving(guide_app(settings)) as guide_url,
            httpx.Client(
                base_url=guide_url,
                trust_env=False,
                timeout=5,
                headers={"origin": "http://testserver"},
            ) as client,
        ):
            identity = client.post("/guide/voice/api/v1/identity?location=ivanovo")
            assert identity.status_code == 200
            key = str(uuid4())
            response = client.post(
                "/guide/voice/api/v1/submissions",
                params={"location": city["slug"], "content_epoch": 1, "idempotency_key": key},
                headers={"content-type": "audio/wav"},
                content=(audio[i : i + 8192] for i in range(0, len(audio), 8192))
                if chunked
                else audio,
            )
            assert response.status_code == 202, response.text
    with ve.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM submissions")).scalar_one() == 1
