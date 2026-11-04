import subprocess
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_guide.main import create_app as guide_app
from involta_voice.contracts import OutboxEvent
from involta_voice.errors import VoiceError
from involta_voice.main import create_app
from involta_voice.provider import FakeProvider
from involta_voice.service import VoiceService
from involta_voice.worker import Worker

from .test_jobs import setup
from .test_voice_guide import client_for
from .test_voice_service import metadata, recording
from .test_voice_service import voice_database as voice_database
from .test_web import login


@pytest.mark.parametrize("seconds", [0.5, 4.999, 5.0, 5.001])
def test_new_intake_minimum_is_measured_before_any_submission_or_job(voice_database, seconds):
    settings, engine = voice_database
    with TestClient(create_app(settings)) as client:
        result = client.post(
            "/internal/v1/submissions",
            headers={"authorization": "Bearer " + settings.service_secret.get_secret_value()},
            data={"metadata": metadata().model_dump_json()},
            files={"audio": ("voice.wav", recording(seconds), "audio/wav")},
        )
    assert result.status_code == (202 if seconds >= 5 else 422), result.text
    if seconds < 5:
        assert result.json()["error"]["code"] == "audio_too_short"
        assert result.json()["error"]["min_duration_seconds"] == 5
        assert list((settings.data_dir / "audio").iterdir()) == []
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM submissions")).scalar_one() == 0
            assert conn.execute(text("SELECT count(*) FROM stages")).scalar_one() == 0


@pytest.mark.parametrize(
    "mime,extension,codec",
    [
        ("audio/webm", "webm", "libopus"),
        ("audio/ogg", "ogg", "libopus"),
        ("audio/mp4", "m4a", "aac"),
    ],
)
@pytest.mark.parametrize("seconds", [4.999, 5.0, 5.001])
def test_encoded_container_boundaries_do_not_count_encoder_padding(
    voice_database, tmp_path, mime, extension, codec, seconds
):
    settings, engine = voice_database
    source = tmp_path / "source.wav"
    source.write_bytes(recording(seconds))
    encoded = tmp_path / ("voice." + extension)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(source), "-c:a", codec, str(encoded)],
        check=True,
        capture_output=True,
    )
    service = VoiceService(engine, settings)
    if seconds < 5:
        with pytest.raises(VoiceError) as error:
            service.submit(metadata(), encoded.read_bytes(), mime)
        assert error.value.code == "audio_too_short"
        assert list((settings.data_dir / "audio").iterdir()) == []
    else:
        result = service.submit(metadata(), encoded.read_bytes(), mime)
        assert result["state"] == "accepted"


def test_old_accepted_short_recording_replays_and_delivers_unchanged(voice_database, monkeypatch):
    settings, engine = voice_database
    service = VoiceService(engine, settings)
    meta = metadata()
    audio = recording(2)
    original = service.storage.save
    with monkeypatch.context() as patch:
        patch.setattr(
            service.storage,
            "save",
            lambda content, mime, **kwargs: original(content, mime, name=kwargs.get("name")),
        )
        accepted = service.submit(meta, audio, "audio/wav")
    assert service.submit(meta, audio, "audio/wav")["reference"] == accepted["reference"]
    assert Worker(engine, settings, FakeProvider()).once()
    with engine.connect() as conn:
        event = conn.execute(
            text("SELECT payload FROM outbox WHERE kind='gigaam_ready'")
        ).scalar_one()
        assert OutboxEvent.model_validate({"sequence": 1, **event}).duration == 2
        assert conn.execute(text("SELECT count(*) FROM submissions")).scalar_one() == 1


@pytest.mark.parametrize("admin", [False, True])
def test_guide_raw_gateway_preserves_short_error_for_visitor_and_admin(
    database, voice_database, admin
):
    settings, engine, _, queries, city, _, _ = setup(database)
    voice_settings, voice_engine = voice_database
    settings = settings.model_copy(
        update={"voice_service_secret": voice_settings.service_secret, "voice_enabled": True}
    )
    app = guide_app(settings)
    app.state.voice_client = client_for(settings, voice_settings)
    headers = {"origin": "http://testserver", "content-type": "audio/wav"}
    with TestClient(app) as client:
        if admin:
            login(client)
        identity = client.post(
            "/guide/voice/api/v1/identity?location=ivanovo", headers=headers
        ).json()
        result = client.post(
            "/guide/voice/api/v1/submissions",
            params={
                "location": city["slug"],
                "content_epoch": identity["content_epoch"],
                "idempotency_key": str(uuid4()),
            },
            headers=headers,
            content=recording(4.999),
        )
        assert result.status_code == 422, result.text
        assert result.json()["error"]["code"] == "audio_too_short"
        assert result.json()["error"]["min_duration_seconds"] == 5
    assert queries.rows("reviews") == queries.rows("jobs") == []
    assert not list((settings.data_dir / "audio").glob("*"))
    with voice_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM submissions")).scalar_one() == 0
