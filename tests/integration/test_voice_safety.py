import json
import subprocess
from uuid import uuid4

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


def test_daily_budget_stops_analysis_without_losing_transcript(voice_database):
    settings, engine = voice_database
    settings = settings.model_copy(update={"daily_budget_usd": 1})
    service = VoiceService(engine, settings)
    ref = service.submit(metadata(), recording(), "audio/wav")["reference"]
    worker = Worker(engine, settings, FakeProvider())
    assert worker.once() and worker.once()
    assert service.status(ref)["transcript"]
    assert service.status(ref)["state"] == "analysis_failed"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM provider_calls")).scalar_one() == 1


def test_revalidation_and_new_run_are_idempotent_and_keep_snapshots(voice_database):
    settings, engine = voice_database
    service = VoiceService(engine, settings)
    ref = service.submit(metadata(), recording(), "audio/wav")["reference"]
    worker = Worker(engine, settings, FakeProvider())
    assert worker.once()
    assert worker.claim()
    raw = FakeProvider().call("analysis", {}, b"", "audio/wav")
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE stages SET state='failed',raw_response=CAST(:raw AS jsonb),lease_token=NULL,lease_until=NULL WHERE kind='analysis'"
            ),
            {"raw": json.dumps(raw)},
        )

    class NoCalls(FakeProvider):
        def call(self, *args):
            raise AssertionError("Revalidation cannot call OpenAI")

    worker = Worker(engine, settings, NoCalls())
    worker.revalidate(ref, 1)
    worker.revalidate(ref, 1)
    with engine.connect() as conn:
        old = conn.execute(text("SELECT snapshot FROM runs")).scalar_one()
        assert (
            conn.execute(
                text("SELECT count(*) FROM outbox WHERE kind='analysis_ready'")
            ).scalar_one()
            == 1
        )
    key = str(uuid4())
    assert service.new_run(ref, key)["run"] == 2
    assert service.new_run(ref, key)["run"] == 2
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM runs")).scalar_one() == 2
        assert conn.execute(text("SELECT snapshot FROM runs WHERE number=1")).scalar_one() == old
        assert (
            conn.execute(
                text("SELECT count(*) FROM stages WHERE kind='transcription'")
            ).scalar_one()
            == 1
        )


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
