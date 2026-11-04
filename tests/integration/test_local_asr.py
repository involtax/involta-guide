import time

from pydantic import SecretStr
from sqlalchemy import text

from involta_voice.provider import FakeProvider
from involta_voice.service import VoiceService
from involta_voice.worker import Worker

from .test_voice_service import metadata, recording
from .test_voice_service import voice_database as voice_database


def test_transcription_survives_missing_analysis_key_without_audio_network(
    voice_database, monkeypatch
):
    settings, engine = voice_database
    settings = settings.model_copy(update={"fake_provider": False, "openai_api_key": SecretStr("")})
    from involta_voice.asr import LocalAsr

    monkeypatch.setattr(
        LocalAsr,
        "call",
        lambda self, request, audio: {
            "status_code": 200,
            "request_id": None,
            "body": '{"text":"Локальная расшифровка."}',
        },
    )
    service = VoiceService(engine, settings)
    ref = service.submit(metadata(), recording(), "audio/wav")["reference"]
    worker = Worker(engine, settings)
    try:
        assert worker.once()
        assert service.status(ref)["transcript"] == "Локальная расшифровка."
        with engine.connect() as conn:
            call = conn.execute(text("SELECT * FROM provider_calls")).mappings().one()
            assert call["reserved_usd"] is None and call["cost_usd"] is None
        assert worker.once()  # optional analysis has no API key; text is still committed.
        assert service.status(ref)["transcript"] == "Локальная расшифровка."
        assert service.status(ref)["state"] == "analysis_failed"
    finally:
        worker.provider.close()


def test_local_inference_renews_lease_while_running(voice_database):
    settings, engine = voice_database
    settings = settings.model_copy(update={"lease_seconds": 1})
    service = VoiceService(engine, settings)
    ref = service.submit(metadata(), recording(), "audio/wav")["reference"]

    class SlowLocal(FakeProvider):
        def call(self, *args):
            time.sleep(1.3)
            return super().call(*args)

    assert Worker(engine, settings, SlowLocal()).once()
    assert service.status(ref)["transcript"]


def test_interrupted_local_inference_retries_without_cloud_unknown(voice_database):
    settings, engine = voice_database
    service = VoiceService(engine, settings)
    ref = service.submit(metadata(), recording(), "audio/wav")["reference"]
    worker = Worker(engine, settings, FakeProvider())
    assert worker.claim()
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE stages SET request='{\"engine\":\"onnx-asr\"}'::jsonb,lease_until=now()-interval '1 second'"
            )
        )
    assert worker.once()
    assert service.status(ref)["transcript"]
