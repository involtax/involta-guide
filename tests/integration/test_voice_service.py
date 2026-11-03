import io
import json
import os
import wave
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from sqlalchemy import text
from sqlalchemy.engine import make_url

from involta_voice.config import Settings
from involta_voice.contracts import LocationContext, SubmissionMetadata
from involta_voice.db import database, migrate
from involta_voice.main import create_app
from involta_voice.provider import FakeProvider, ProviderFailure
from involta_voice.service import VoiceService
from involta_voice.worker import Worker


@pytest.fixture
def voice_database(tmp_path):
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.fail("TEST_DATABASE_URL required to provision an isolated Voice database/role")
    name = "voice_test_" + uuid4().hex
    password = uuid4().hex
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(name), sql.Literal(password)
            )
        )
        conn.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(name), sql.Identifier(name)
            )
        )
    url = make_url(admin_url).set(database=name, username=name, password=password)
    settings = Settings(
        database_url=url.render_as_string(hide_password=False),
        service_secret="local-voice-test-" + "x" * 40,
        data_dir=tmp_path,
        min_free_bytes=0,
        fake_provider=True,
        asr_beta_enabled=False,
    )
    engine = database(settings)
    try:
        migrate(engine)
        yield settings, engine
    finally:
        engine.dispose()
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))


def recording():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 16000)
    return stream.getvalue()


def metadata(**kwargs):
    return SubmissionMetadata(
        location=LocationContext(id=uuid4(), slug="ivanovo", name="Иваново"),
        visitor="v" * 64,
        ip_fingerprint="i" * 64,
        idempotency_key=uuid4(),
        correlation_id=uuid4(),
        **kwargs,
    )


def test_acceptance_idempotency_ownership_and_service_auth(voice_database):
    settings, engine = voice_database
    data = metadata()
    headers = {"authorization": "Bearer " + settings.service_secret.get_secret_value()}
    with TestClient(create_app(settings)) as client:
        assert client.get("/health/ready").status_code == 200
        assert client.get("/internal/v1/admin/submissions").status_code == 401

        def upload(meta, body=recording()):
            return client.post(
                "/internal/v1/submissions",
                headers=headers,
                data={"metadata": meta.model_dump_json()},
                files={"audio": ("recording.wav", body, "audio/wav")},
            )

        accepted = upload(data)
        assert accepted.status_code == 202, accepted.text
        reference = accepted.json()["reference"]
        assert upload(data).json()["reference"] == reference
        changed = data.model_copy(update={"language": "en"})
        assert upload(changed).status_code == 409
        assert (
            client.get(
                "/internal/v1/submissions/" + reference,
                headers=headers,
                params={"visitor": "other"},
            ).status_code
            == 404
        )
        assert (
            client.get(
                "/internal/v1/submissions/" + reference,
                headers=headers,
                params={"visitor": data.visitor},
            ).status_code
            == 200
        )
        forged = data.model_dump(mode="json")
        forged["actor"] = "admin"
        bad = client.post(
            "/internal/v1/submissions",
            headers=headers,
            data={"metadata": json.dumps(forged)},
            files={"audio": ("x.wav", recording(), "audio/wav")},
        )
        assert bad.status_code == 400
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM submissions")).scalar_one() == 1
    assert len(list((settings.data_dir / "audio").glob("*.wav"))) == 1


def test_transcript_is_committed_before_analysis_failure_and_survives_new_run(voice_database):
    settings, engine = voice_database
    service = VoiceService(engine, settings)
    reference = service.submit(metadata(), recording(), "audio/wav")["reference"]

    class BrokenAnalysis(FakeProvider):
        calls = []

        def call(self, kind, request, audio, mime):
            self.calls.append(kind)
            if kind == "analysis":
                raise ProviderFailure("OpenAI HTTP 403")
            return super().call(kind, request, audio, mime)

    provider = BrokenAnalysis()
    assert Worker(engine, settings, provider).once()
    with engine.connect() as conn:
        event = conn.execute(text("SELECT payload FROM outbox")).scalar_one()
        assert event["kind"] == "transcript_ready" and event["transcript"]
    assert Worker(engine, settings, provider).once()
    status = service.status(reference)
    assert status["transcript"] == "Был здесь сегодня, было вкусно."
    assert status["state"] == "analysis_failed"
    assert (settings.data_dir / "audio").iterdir()
    service.new_run(reference)
    assert Worker(engine, settings, FakeProvider()).once()
    assert service.status(reference)["state"] == "result_ready"
    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM stages WHERE kind='transcription'")
            ).scalar_one()
            == 1
        )
        assert conn.execute(text("SELECT count(*) FROM runs")).scalar_one() == 2
    assert provider.calls == ["transcription", "analysis"]


def test_expired_paid_attempt_is_unknown_not_automatically_submitted_twice(voice_database):
    settings, engine = voice_database
    service = VoiceService(engine, settings)
    reference = service.submit(metadata(), recording(), "audio/wav")["reference"]
    worker = Worker(engine, settings, FakeProvider())
    assert worker.claim()
    with engine.begin() as conn:
        conn.execute(text("UPDATE stages SET lease_until=now()-interval '1 second'"))
    assert not worker.once()
    assert service.status(reference)["state"] == "transcription_unknown"


def test_saved_transcription_response_recovers_without_new_provider_call(voice_database):
    settings, engine = voice_database
    service = VoiceService(engine, settings)
    reference = service.submit(metadata(), recording(), "audio/wav")["reference"]
    worker = Worker(engine, settings, FakeProvider())
    stage = worker.claim()
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE stages SET raw_response=CAST(:raw AS jsonb),lease_until=now()-interval '1 second'"
            ),
            {
                "raw": json.dumps(
                    {
                        "status_code": 200,
                        "body": json.dumps({"text": "Был в Огоньке сегодня, было вкусно"}),
                    }
                )
            },
        )

    class NoPaidCalls(FakeProvider):
        def call(self, *args, **kwargs):
            raise AssertionError("Saved transcription must be reused")

    assert Worker(engine, settings, NoPaidCalls()).once()
    assert service.status(reference)["transcript"] == "Был в Огоньке сегодня, было вкусно"
    with pytest.raises(ProviderFailure, match="lease"):
        worker.process(stage)


def test_bad_audio_and_no_action_do_not_create_fake_ratings(voice_database):
    settings, engine = voice_database
    service = VoiceService(engine, settings)
    with pytest.raises(ValueError):
        service.submit(metadata(), b"not audio" * 30, "audio/webm")
    reference = service.submit(metadata(), recording(), "audio/wav")["reference"]
    worker = Worker(engine, settings, FakeProvider())
    assert worker.once() and worker.once()
    with engine.connect() as conn:
        result = conn.execute(text("SELECT result FROM runs")).scalar_one()
        assert result["outcome"] == "no_action" and result["entries"] == []
        assert conn.execute(text("SELECT count(*) FROM outbox")).scalar_one() == 2
    assert service.status(reference)["transcript"]


def test_manual_revalidation_recovers_saved_transcription_without_paid_retry(voice_database):
    settings, engine = voice_database
    service = VoiceService(engine, settings)
    ref = service.submit(metadata(), recording(), "audio/wav")["reference"]
    worker = Worker(engine, settings, FakeProvider())
    assert worker.claim()
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE stages SET state='unknown', raw_response=CAST(:raw AS jsonb)"),
            {
                "raw": json.dumps(
                    {"status_code": 200, "body": json.dumps({"text": "Исходный отзыв"})}
                )
            },
        )
    worker.revalidate(ref, 1)
    assert service.status(ref)["transcript"] == "Исходный отзыв"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM provider_calls")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM outbox")).scalar_one() == 1
        assert (
            conn.execute(text("SELECT state FROM stages WHERE kind='analysis'")).scalar_one()
            == "queued"
        )
