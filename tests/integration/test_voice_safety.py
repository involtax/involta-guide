import json
import subprocess

import pytest
from sqlalchemy import text

from involta_voice.provider import FakeProvider
from involta_voice.service import VoiceService
from involta_voice.worker import Worker

from .test_voice_service import metadata, recording
from .test_voice_service import voice_database as voice_database


@pytest.mark.parametrize(
    "status,state",
    [(403, "transcription_failed"), (429, "accepted"), (500, "transcription_unknown")],
)
def test_provider_errors_are_saved_without_automatic_duplicate_unknown_post(
    voice_database, status, state
):
    settings, engine = voice_database
    service = VoiceService(engine, settings)
    ref = service.submit(metadata(), recording(), "audio/wav")["reference"]

    class FailedProvider(FakeProvider):
        calls = 0

        def call(self, *args):
            self.calls += 1
            return {
                "status_code": status,
                "request_id": "req_preserved",
                "body": json.dumps({"error": {"message": "fixture"}}),
            }

    provider = FailedProvider()
    worker = Worker(engine, settings, provider)
    assert worker.once()
    assert service.status(ref)["state"] == state
    assert not worker.once()
    with engine.connect() as conn:
        raw = conn.execute(text("SELECT response FROM provider_calls")).scalar_one()
        assert raw["request_id"] == "req_preserved"
    assert provider.calls == 1


@pytest.mark.parametrize(
    "mime,extension,codec",
    [
        ("audio/webm", "webm", "libopus"),
        ("audio/ogg", "ogg", "libopus"),
        ("audio/mp4", "m4a", "aac"),
    ],
)
def test_backend_accepts_measured_supported_containers(
    voice_database, tmp_path, mime, extension, codec
):
    settings, engine = voice_database
    source = tmp_path / "source.wav"
    source.write_bytes(recording())
    target = tmp_path / ("converted." + extension)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(source), "-c:a", codec, str(target)],
        check=True,
        capture_output=True,
    )
    result = VoiceService(engine, settings).submit(metadata(), target.read_bytes(), mime)
    assert result["state"] == "accepted"


def test_disk_failure_does_not_acknowledge_or_create_submission(voice_database, monkeypatch):
    settings, engine = voice_database
    settings = settings.model_copy(update={"min_free_bytes": 10**30})
    with pytest.raises(ValueError, match="места"):
        VoiceService(engine, settings).submit(metadata(), recording(), "audio/wav")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM submissions")).scalar_one() == 0
