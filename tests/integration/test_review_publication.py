import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from involta_guide.infrastructure.voice.consumer import Consumer
from involta_guide.infrastructure.voice.read_model import VoiceReadModel
from involta_guide.main import create_app
from involta_voice.contracts import LocationContext
from involta_voice.db import migrate
from involta_voice.provider import FakeProvider, ProviderFailure
from involta_voice.service import VoiceService
from involta_voice.worker import Worker

from .test_jobs import setup
from .test_sherpa_comparison import add_saved_comparison
from .test_voice_guide import client_for
from .test_voice_service import metadata, recording
from .test_voice_service import voice_database as voice_database
from .voice_fixture import imported


@pytest.mark.parametrize("failure", ["empty", "failed", "unknown"])
def test_primary_failure_unpublishes_but_retains_audio_and_logs(database, voice_database, failure):
    settings, engine, _, queries, city, _, _ = setup(database)
    vs, ve = voice_database
    vs = vs.model_copy(update={"asr_beta_enabled": True})
    settings = settings.model_copy(update={"voice_service_secret": vs.service_secret})
    meta = metadata().model_copy(
        update={
            "location": LocationContext(
                id=city["id"], slug=city["slug"], name=city["name"], content_epoch=1
            )
        }
    )
    service = VoiceService(ve, vs)
    reference = service.submit(meta, recording(), "audio/wav")["reference"]
    add_saved_comparison(ve, reference)
    consumer = Consumer(engine, client_for(settings, vs))
    assert consumer.once() == 1
    assert queries.get("reviews", reference)["publication_status"] == 1
    assert not VoiceReadModel(engine).public_texts(city["id"])
    assert (
        VoiceReadModel(engine).public_texts(city["id"], visitor=meta.visitor)[0]["state"]
        == "accepted"
    )

    class PrimaryFailure(FakeProvider):
        def call(self, kind, request, audio, mime):
            if request.get("variant", "onnx") == "sherpa_hotwords_strong":
                if failure == "empty":
                    return {"status_code": 200, "body": json.dumps({"text": "  "})}
                raise ProviderFailure("GigaAM test failure", unknown=failure == "unknown")
            return super().call(kind, request, audio, mime)

    worker = Worker(ve, vs, PrimaryFailure())
    assert worker.once()
    assert consumer.once() == 1
    stored = queries.get("reviews", reference)
    assert stored["publication_status"] == 0 and stored["status"] == "failed" and stored["error"]
    assert VoiceReadModel(engine).public_texts(city["id"]) == []
    assert worker.once()  # A successful comparison cannot publish a failed primary.
    assert consumer.once() == 0
    assert queries.get("reviews", reference)["publication_status"] == 0
    with ve.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM submissions")).scalar_one() == 1
        failure_event = conn.execute(
            text("SELECT payload FROM outbox WHERE kind='gigaam_failed'")
        ).scalar_one()
        assert failure_event["error"]
    with TestClient(create_app(settings)) as client:
        page = client.get("/guide/ivanovo")
        assert "Не удалось расшифровать" not in page.text
        feed = client.get(
            "/guide/voice/api/v1/reviews", params={"location": city["slug"], "pending": reference}
        ).json()
        assert feed["rows"] == [] and feed["publication_statuses"] == {reference: 0}
        assert "error" not in feed
        assert (
            client.get(
                "/guide/voice/api/v1/reviews",
                params={"location": city["slug"], "pending": "invalid"},
            ).status_code
            == 422
        )
        assert client.get(f"/guide/logs/reviews/ivanovo/{stored['number']}").status_code == 200
    assert list((vs.data_dir / "audio").glob("*.wav"))
    assert consumer.once() == 0


def test_comparison_failure_does_not_unpublish_primary_text(database, voice_database):
    settings, engine, _, queries, city, _, _ = setup(database)
    vs, ve = voice_database
    vs = vs.model_copy(update={"asr_beta_enabled": True})
    settings = settings.model_copy(update={"voice_service_secret": vs.service_secret})
    meta = metadata().model_copy(
        update={
            "location": LocationContext(
                id=city["id"], slug=city["slug"], name=city["name"], content_epoch=1
            )
        }
    )
    reference = VoiceService(ve, vs).submit(meta, recording(), "audio/wav")["reference"]

    add_saved_comparison(ve, reference)

    class ComparisonFailure(FakeProvider):
        def call(self, kind, request, audio, mime):
            if request.get("variant", "onnx") != "sherpa_hotwords_strong":
                raise ProviderFailure("Comparison test failure")
            return super().call(kind, request, audio, mime)

    worker = Worker(ve, vs, ComparisonFailure())
    assert worker.once() and worker.once()
    assert Consumer(engine, client_for(settings, vs)).once() == 2
    stored = queries.get("reviews", reference)
    assert stored["publication_status"] == 1
    assert not VoiceReadModel(engine).public_texts(city["id"])
    assert (
        VoiceReadModel(engine).public_texts(city["id"], visitor=meta.visitor)[0]["transcript"]
        == stored["transcript"]
    )
    with ve.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM outbox WHERE kind='gigaam_failed'")
            ).scalar_one()
            == 0
        )


def test_existing_failed_primary_is_delivered_after_version_seven_migration(
    database, voice_database
):
    settings, engine, _, queries, city, _, _ = setup(database)
    vs, ve = voice_database
    settings = settings.model_copy(update={"voice_service_secret": vs.service_secret})
    meta = metadata().model_copy(
        update={
            "location": LocationContext(
                id=city["id"], slug=city["slug"], name=city["name"], content_epoch=1
            )
        }
    )
    reference = VoiceService(ve, vs).submit(meta, recording(), "audio/wav")["reference"]
    consumer = Consumer(engine, client_for(settings, vs))
    assert consumer.once() == 1
    # Emulate a terminal primary failure from the previous release, without a failure outbox event.
    with ve.begin() as conn:
        conn.execute(
            text(
                "UPDATE stages SET state='failed',error='Previously unrecognized' WHERE kind='transcription'"
            )
        )
        conn.execute(text("DELETE FROM voice_schema_versions WHERE version=7"))
    migrate(ve)
    assert consumer.once() == 1
    assert queries.get("reviews", reference)["publication_status"] == 0
    migrate(ve)
    assert consumer.once() == 0
    with ve.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM outbox WHERE kind='gigaam_failed'")
            ).scalar_one()
            == 1
        )


def test_publication_values_and_out_of_order_failure_do_not_override_text_or_deletion(database):
    settings, engine, _, queries, city, reference, event = imported(database)
    consumer = Consumer(engine, None)
    failed = {
        **event,
        "event_id": str(uuid4()),
        "sequence": 2,
        "kind": "gigaam_failed",
        "transcript": None,
        "error": "Late failure",
    }
    assert consumer.import_event(failed) == 1
    assert queries.get("reviews", reference)["publication_status"] == 1
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE reviews SET publication_status=2 WHERE id=:id"), {"id": reference}
        )
    assert VoiceReadModel(engine).public_texts(city["id"]) == []
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            text("UPDATE reviews SET publication_status=3 WHERE id=:id"), {"id": reference}
        )
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE reviews SET publication_status=0,status='deleted' WHERE id=:id"),
            {"id": reference},
        )
    for n, kind in enumerate(("recording_received", "gigaam_ready", "gigaam_failed"), 3):
        consumer.import_event({**event, "event_id": str(uuid4()), "sequence": n, "kind": kind})
    stored = queries.get("reviews", reference)
    assert stored["publication_status"] == 0 and stored["status"] == "deleted"
