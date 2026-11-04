import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_voice.db import migrate
from involta_voice.main import create_app
from involta_voice.provider import FakeProvider, ProviderFailure
from involta_voice.recognition import report, reports
from involta_voice.service import VoiceService
from involta_voice.worker import Worker

from .test_voice_service import metadata, recording
from .test_voice_service import voice_database as voice_database


class Pair(FakeProvider):
    def __init__(self, failed=None):
        self.failed = failed
        self.calls = []

    def call(self, kind, request, audio, mime):
        self.calls.append(request["model"])
        if request["model"] == self.failed:
            raise ProviderFailure("local model failed")
        result = super().call(kind, request, audio, mime)
        result["body"] = json.dumps(
            {
                "text": "Текст GigaAM" if request["model"] == "gigaam" else "Только Parakeet",
                "elapsed_seconds": 1,
            }
        )
        return result


@pytest.mark.parametrize("failed", [None, "gigaam", "parakeet"])
def test_primary_only_publication_and_independent_model_results(voice_database, failed):
    settings, engine = voice_database
    settings = settings.model_copy(update={"asr_beta_enabled": True})
    service = VoiceService(engine, settings)
    ref = service.submit(metadata(), recording(), "audio/wav")["reference"]
    provider = Pair(failed)
    worker = Worker(engine, settings, provider)
    assert all(worker.once() for _ in range(6))
    assert not worker.once()
    status = service.status(ref)
    assert status["transcript"] == (None if failed == "gigaam" else "Текст GigaAM")
    assert status["state"] == ("transcription_failed" if failed == "gigaam" else "result_ready")
    assert "parakeet" not in json.dumps(status).lower()
    pair = reports(engine, [ref])[ref]
    assert {r["engine"] for r in pair} == {"gigaam", "parakeet"}
    assert len(pair) == 6
    assert sum(r["state"] == "failed" for r in pair) == (3 if failed else 0)
    assert len(report(engine, ref)["candidates"]) == (3 if failed else 6)
    assert provider.calls[:2] == ["gigaam", "parakeet"]
    assert provider.calls.count("gigaam") == provider.calls.count("parakeet") == 3
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT count(*) FROM outbox WHERE kind='gigaam_ready'")
        ).scalar_one() == (failed != "gigaam")
        assert (
            conn.execute(text("SELECT count(*) FROM stages WHERE kind='analysis'")).scalar_one()
            == 0
        )


def test_saved_secondary_response_recovers_without_provider(voice_database):
    settings, engine = voice_database
    settings = settings.model_copy(update={"asr_beta_enabled": True})
    service = VoiceService(engine, settings)
    ref = service.submit(metadata(), recording(), "audio/wav")["reference"]
    worker = Worker(engine, settings, FakeProvider())
    assert worker.once()
    assert worker.claim()["kind"] == "comparison"
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE stages SET raw_response=CAST(:raw AS jsonb),lease_until=now()-interval '1 second' WHERE kind='comparison'"
            ),
            {"raw": json.dumps({"status_code": 200, "body": '{"text":"Второй результат"}'})},
        )

    class NoCalls:
        def call(self, *args):
            raise AssertionError("Must reuse saved response")

    assert Worker(engine, settings, NoCalls()).once()
    assert reports(engine, [ref])[ref][1]["text"] == "Второй результат"
    assert service.status(ref)["transcript"] != "Второй результат"


def test_private_batch_results_and_deleted_manual_apis(voice_database):
    settings, engine = voice_database
    ref = VoiceService(engine, settings).submit(metadata(), recording(), "audio/wav")["reference"]
    with TestClient(create_app(settings)) as client:
        assert client.post("/internal/v1/admin/recognitions", json=[ref]).status_code == 401
        headers = {"authorization": "Bearer " + settings.service_secret.get_secret_value()}
        response = client.post("/internal/v1/admin/recognitions", json=[ref], headers=headers)
        assert response.status_code == 200 and len(response.json()[ref]) == 2
        assert (
            client.post(
                "/internal/v1/admin/recognitions", json=[ref] * 31, headers=headers
            ).status_code
            == 400
        )
        for suffix in ("/asr-runs", "/asr-runs/1/evaluation", "/runs", "/runs/1/revalidate"):
            assert (
                client.post(
                    "/internal/v1/admin/submissions/" + ref + suffix, headers=headers, json={}
                ).status_code
                == 404
            )


@pytest.mark.parametrize("with_primary", [False, True])
def test_populated_migration_preserves_sources_and_replays_only_proven_primary(
    voice_database, with_primary
):
    settings, engine = voice_database
    settings = settings.model_copy(update={"asr_beta_enabled": True})
    service = VoiceService(engine, settings)
    ref = service.submit(metadata(), recording(), "audio/wav")["reference"]
    worker = Worker(engine, settings, Pair(None if with_primary else "gigaam"))
    while worker.once():
        pass
    with engine.begin() as conn:
        # A database at released version 5 with a Parakeet fallback and human judgments.
        conn.execute(text("DELETE FROM asr_candidates WHERE variant<>'onnx'"))
        conn.execute(text("DELETE FROM stages WHERE kind LIKE 'sherpa_%'"))
        conn.execute(text("ALTER TABLE asr_candidates DROP CONSTRAINT asr_candidates_pkey"))
        conn.execute(text("ALTER TABLE asr_candidates DROP COLUMN variant"))
        conn.execute(text("ALTER TABLE asr_candidates ADD PRIMARY KEY(submission,run,engine)"))
        conn.execute(text("DELETE FROM voice_schema_versions WHERE version>=6"))
        conn.execute(
            text("CREATE TABLE asr_evaluations(reference_text text,preferred_engine text)")
        )
        conn.execute(text("INSERT INTO asr_evaluations VALUES('human reference','parakeet')"))
        conn.execute(text("CREATE TABLE asr_evaluation_events(before_data jsonb,after_data jsonb)"))
        conn.execute(text("INSERT INTO asr_evaluation_events VALUES('{}','{}')"))
        conn.execute(text("UPDATE submissions SET transcript='Historical fallback'"))
        conn.execute(
            text(
                "DELETE FROM outbox WHERE kind IN ('recording_received','gigaam_ready','gigaam_failed')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO delivery_ack VALUES('guide',123) ON CONFLICT(consumer) DO UPDATE SET sequence=123"
            )
        )
        conn.execute(text("SELECT setval(pg_get_serial_sequence('outbox','sequence'),123)"))
        candidates = (
            conn.execute(text("SELECT * FROM asr_candidates ORDER BY engine")).mappings().all()
        )
        audio = conn.execute(text("SELECT audio FROM submissions")).scalar_one()
    migrate(engine)
    migrate(engine)
    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT transcript FROM submissions")).scalar_one()
            == "Historical fallback"
        )
        assert conn.execute(
            text("SELECT * FROM asr_candidates ORDER BY engine")
        ).mappings().all() == [{**dict(candidate), "variant": "onnx"} for candidate in candidates]
        assert conn.execute(text("SELECT to_regclass('asr_evaluations')")).scalar_one() is None
        assert (
            conn.execute(text("SELECT to_regclass('asr_evaluation_events')")).scalar_one() is None
        )
        events = (
            conn.execute(text("SELECT sequence,payload FROM outbox ORDER BY sequence"))
            .mappings()
            .all()
        )
        assert all(e["sequence"] > 123 for e in events)
        assert [e["payload"]["kind"] for e in events] == (
            ["recording_received", "gigaam_ready"]
            if with_primary
            else ["recording_received", "gigaam_failed"]
        )
        assert all(e["payload"]["transcript"] != "Только Parakeet" for e in events)
    assert service.storage.path(audio).exists()
    assert service.status(ref)["transcript"] == ("Текст GigaAM" if with_primary else None)


@pytest.mark.parametrize("failed", [None, "gigaam"])
def test_frozen_pre_transition_parakeet_primary_never_becomes_public_source(voice_database, failed):
    settings, engine = voice_database
    settings = settings.model_copy(update={"asr_beta_enabled": True})
    service = VoiceService(engine, settings)
    ref = service.submit(metadata(), recording(), "audio/wav")["reference"]
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE runs SET snapshot=jsonb_set(snapshot,'{transcription_model}','\"parakeet\"')"
            )
        )
    worker = Worker(engine, settings, Pair(failed))
    assert worker.once()
    assert service.status(ref)["transcript"] is None
    assert worker.once()
    status = service.status(ref)
    assert status["transcript"] == (None if failed else "Текст GigaAM")
    assert status["state"] == ("transcription_failed" if failed else "result_ready")
    assert "parakeet" not in json.dumps(status).lower()
