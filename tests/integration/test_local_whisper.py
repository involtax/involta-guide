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
    settings = settings.model_copy(
        update={"fake_provider": False, "openai_api_key": SecretStr(""), "daily_budget_usd": 0.01}
    )
    from involta_voice.whisper import LocalWhisper

    monkeypatch.setattr(
        LocalWhisper,
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
            assert call["reserved_usd"] == 0 and call["cost_usd"] == 0
        assert worker.once()  # optional analysis is budget-blocked; text is still committed.
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
                "UPDATE stages SET request='{\"engine\":\"faster-whisper\"}'::jsonb,lease_until=now()-interval '1 second'"
            )
        )
    assert worker.once()
    assert service.status(ref)["transcript"]


def test_shared_guide_transcription_has_no_duplicate_review_or_analysis(voice_database):
    from fastapi.testclient import TestClient

    from involta_voice.main import create_app

    settings, engine = voice_database
    meta = metadata().model_copy(update={"actor": "admin"})
    headers = {"authorization": "Bearer " + settings.service_secret.get_secret_value()}
    with TestClient(create_app(settings)) as client:

        def upload(path):
            return client.post(
                path,
                headers=headers,
                data={"metadata": meta.model_dump_json()},
                files={"audio": ("recording.wav", recording(), "audio/wav")},
            )

        assert upload("/internal/v1/submissions").status_code == 400
        accepted = upload("/internal/v1/guide-transcriptions")
        assert accepted.status_code == 202, accepted.text
        ref = accepted.json()["reference"]
        assert Worker(engine, settings, FakeProvider()).once()
        status = VoiceService(engine, settings).status(ref)
        assert status["state"] == "result_ready" and status["transcript"]
        assert upload("/internal/v1/guide-transcriptions").json()["reference"] == ref
        assert (
            client.get(
                "/internal/v1/submissions", headers=headers, params={"visitor": meta.visitor}
            ).json()["rows"]
            == []
        )
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM outbox")).scalar_one() == 0
        assert (
            conn.execute(text("SELECT count(*) FROM stages WHERE kind='analysis'")).scalar_one()
            == 0
        )


def test_trusted_guide_executor_queues_speech_to_same_voice_worker(
    database, voice_database, monkeypatch
):
    from involta_guide.application.services.reviews import ReviewService
    from involta_guide.infrastructure.ai.openai import OpenAIProvider as GuideAI
    from involta_guide.infrastructure.security import Security
    from involta_guide.infrastructure.storage.audio import FileAudioStorage
    from involta_guide.infrastructure.voice import client as voice_client_module

    from .test_jobs import setup
    from .test_voice_guide import client_for

    settings, engine, uow, queries, city, _, _ = setup(database)
    voice_settings, voice_engine = voice_database
    settings = settings.model_copy(update={"voice_service_secret": voice_settings.service_secret})
    principal = Security(settings, uow).principal(
        Security(settings, uow).login("test-password-123456", "test")
    )
    storage = FileAudioStorage(settings.data_dir, settings.max_audio_bytes)
    review = ReviewService(uow, storage, 100).submit(
        city["id"],
        "test-admin",
        "i" * 64,
        "test",
        recording(),
        "audio/wav",
        admin_session=principal["id"],
    )
    job = queries.rows("jobs", {"review_id": review})[0]
    remote = client_for(settings, voice_settings)

    class Bridge:
        def json(self, method, path, **kwargs):
            result = remote.json(method, path, **kwargs)
            if path == "/guide-transcriptions":
                Worker(voice_engine, voice_settings, FakeProvider()).once()
            return result

    monkeypatch.setattr(voice_client_module, "VoiceClient", lambda config: Bridge())
    provider = GuideAI(settings, engine, storage)
    try:
        assert (
            provider.transcribe(queries.get("reviews", review)["audio"], "audio/wav", job["id"])
            == "Был здесь сегодня, было вкусно."
        )
    finally:
        provider.close()
    assert len(queries.rows("reviews")) == 1
    with voice_engine.connect() as conn:
        assert (
            conn.execute(text("SELECT metadata->>'actor' FROM submissions")).scalar_one() == "admin"
        )
        assert conn.execute(text("SELECT count(*) FROM outbox")).scalar_one() == 0
