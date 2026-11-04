import hashlib
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_guide.infrastructure.voice.consumer import Consumer
from involta_guide.main import create_app
from involta_voice.contracts import LocationContext
from involta_voice.db import migrate
from involta_voice.provider import FakeProvider, ProviderFailure
from involta_voice.recognition import reports
from involta_voice.service import VoiceService
from involta_voice.variants import RESULTS
from involta_voice.worker import Worker

from .test_jobs import setup
from .test_voice_guide import client_for
from .test_voice_service import metadata, recording
from .test_voice_service import voice_database as voice_database
from .test_web import login


class SixResults(FakeProvider):
    def __init__(self, failed=None):
        self.failed = failed
        self.calls = []

    def call(self, kind, request, audio, mime):
        variant = request.get("variant", "onnx")
        self.calls.append(
            (
                request["model"],
                variant,
                request.get("dictionary", []),
                hashlib.sha256(audio).hexdigest(),
            )
        )
        if (request["model"], variant) == self.failed:
            raise ProviderFailure("Independent recognition failure")
        return {
            "status_code": 200,
            "body": json.dumps({"text": f"{request['model']} / {variant}", "is_fake": True}),
        }


def test_six_results_share_audio_and_freeze_dictionary_with_idempotent_intake(voice_database):
    settings, engine = voice_database
    settings = settings.model_copy(update={"asr_beta_enabled": True})
    meta = metadata()
    meta.location.recognition_dictionary = ["Алхимист", "Огонёк", "Сыроварня"]
    service = VoiceService(engine, settings)
    audio = recording()
    reference = service.submit(meta, audio, "audio/wav")["reference"]
    assert service.submit(meta, audio, "audio/wav")["reference"] == reference
    queued = reports(engine, [reference])[reference]
    assert len(queued) == 6 and all(r["state"] == "queued" for r in queued)
    assert [r["dictionary"] for r in queued if r["variant"] == "sherpa_hotwords"] == [
        meta.location.recognition_dictionary
    ] * 2
    meta.location.recognition_dictionary.append("Добавлено позже")
    provider = SixResults()
    worker = Worker(engine, settings, provider)
    assert all(worker.once() for _ in range(6)) and not worker.once()
    assert len(provider.calls) == 6
    assert {call[3] for call in provider.calls} == {hashlib.sha256(audio).hexdigest()}
    rows = reports(engine, [reference])[reference]
    assert [(r["engine"], r["variant"]) for r in rows] == list(RESULTS)
    assert all(r["state"] == "completed" for r in rows)
    assert rows[3]["dictionary"] == rows[5]["dictionary"] == ["Алхимист", "Огонёк", "Сыроварня"]
    assert all(not r["dictionary"] for r in rows if r["variant"] != "sherpa_hotwords")
    assert service.status(reference)["transcript"] == "gigaam / onnx"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM submissions")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM asr_candidates")).scalar_one() == 6
        assert (
            conn.execute(text("SELECT count(*) FROM outbox WHERE kind='gigaam_ready'")).scalar_one()
            == 1
        )


@pytest.mark.parametrize("failed", list(RESULTS)[2:] + [("gigaam", "onnx")])
def test_comparison_failure_is_independent_and_sherpa_never_becomes_public(
    database, voice_database, failed
):
    settings, engine, _, queries, city, _, _ = setup(database)
    vs, ve = voice_database
    vs = vs.model_copy(update={"asr_beta_enabled": True})
    settings = settings.model_copy(update={"voice_service_secret": vs.service_secret})
    meta = metadata().model_copy(
        update={
            "location": LocationContext(
                id=city["id"],
                slug=city["slug"],
                name=city["name"],
                content_epoch=1,
                recognition_dictionary=["Алхимист"],
            )
        }
    )
    service = VoiceService(ve, vs)
    reference = service.submit(meta, recording(), "audio/wav")["reference"]
    consumer = Consumer(engine, client_for(settings, vs))
    assert consumer.once() == 1
    worker = Worker(ve, vs, SixResults(failed))
    assert all(worker.once() for _ in range(6)) and not worker.once()
    assert consumer.once() == 1 and consumer.once() == 0
    rows = reports(ve, [reference])[reference]
    assert sum(r["state"] == "failed" for r in rows) == 1
    failed_row = next(r for r in rows if (r["engine"], r["variant"]) == failed)
    assert failed_row["error"] == "Independent recognition failure"
    stored = queries.get("reviews", reference)
    assert stored["publication_status"] == (0 if failed == ("gigaam", "onnx") else 1)
    assert stored["transcript"] == (None if failed == ("gigaam", "onnx") else "gigaam / onnx")
    assert list((vs.data_dir / "audio").glob("*.wav"))


def test_dictionary_contains_all_city_restaurants_and_admin_displays_frozen_input(
    database, voice_database
):
    settings, engine, _, queries, city, _, _ = setup(database)
    vs, ve = voice_database
    vs = vs.model_copy(update={"asr_beta_enabled": True})
    settings = settings.model_copy(
        update={"voice_service_secret": vs.service_secret, "voice_enabled": True}
    )
    names = [f"Ресторан {i:03}" for i in range(205)] + ["Алхимист"]
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO restaurants(id,location_id,name,cuisine,description,maps_url,active) VALUES(:id,:city,:name,'','','',0)"
            ),
            [{"id": str(uuid4()), "city": city["id"], "name": name} for name in names],
        )
    app = create_app(settings)
    app.state.voice_client = client_for(settings, vs)
    with TestClient(app) as client:
        headers = {"origin": "http://testserver"}
        assert (
            client.post(
                "/guide/voice/api/v1/identity?location=ivanovo", headers=headers
            ).status_code
            == 200
        )
        response = client.post(
            "/guide/voice/api/v1/submissions?location=ivanovo&content_epoch=1",
            headers=headers,
            data={"idempotency_key": str(uuid4())},
            files={"audio": ("voice.wav", recording(), "audio/wav")},
        )
        assert response.status_code == 202, response.text
        assert response.json()["location"]["recognition_dictionary"] == []
        reference = response.json()["reference"]
        with ve.connect() as conn:
            saved = conn.execute(text("SELECT metadata FROM submissions")).scalar_one()
            assert saved["location"]["recognition_dictionary"] == sorted(names)
        with engine.begin() as conn:
            conn.execute(text("UPDATE restaurants SET name='Переименован' WHERE name='Алхимист'"))
        worker = Worker(ve, vs, SixResults())
        assert all(worker.once() for _ in range(6))
        Consumer(engine, client_for(settings, vs)).once()
        login(client)
        page = client.get("/guide/admin/reviews/ivanovo")
        assert page.status_code == 200
        assert page.text.count('class="recognition-dictionary"') == 6
        assert page.text.count("Алхимист") == 2
        assert "Переименован" not in page.text
        assert page.text.count("Sherpa · со словарём") == 2
        assert "Ресторан 204" in page.text
        public = client.get("/guide/voice/api/v1/reviews?location=ivanovo").json()
        assert public["rows"][0]["transcript"] == "gigaam / onnx"
        assert "sherpa" not in json.dumps(public).lower()
        with ve.connect() as conn:
            before = conn.execute(text("SELECT count(*) FROM provider_calls")).scalar_one()
        assert client.get("/guide/admin/reviews/ivanovo").status_code == 200
        with ve.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM provider_calls")).scalar_one() == before
        assert queries.get("reviews", reference)["publication_status"] == 1


def test_migration_preserves_original_results_and_does_not_schedule_historical_audio(
    voice_database,
):
    settings, engine = voice_database
    service = VoiceService(engine, settings)
    reference = service.submit(metadata(), recording(), "audio/wav")["reference"]
    assert Worker(engine, settings, SixResults()).once()
    with engine.begin() as conn:
        old = dict(conn.execute(text("SELECT * FROM asr_candidates")).mappings().one())
        old.pop("variant")
        audio = conn.execute(text("SELECT audio FROM submissions")).scalar_one()
        conn.execute(text("DELETE FROM voice_schema_versions WHERE version=8"))
        conn.execute(text("ALTER TABLE asr_candidates DROP CONSTRAINT asr_candidates_pkey"))
        conn.execute(text("ALTER TABLE asr_candidates DROP COLUMN variant"))
        conn.execute(text("ALTER TABLE asr_candidates ADD PRIMARY KEY(submission,run,engine)"))
    migrate(engine)
    migrate(engine)
    with engine.connect() as conn:
        assert dict(conn.execute(text("SELECT * FROM asr_candidates")).mappings().one()) == {
            **old,
            "variant": "onnx",
        }
        assert (
            conn.execute(
                text("SELECT count(*) FROM stages WHERE kind LIKE 'sherpa_%'")
            ).scalar_one()
            == 0
        )
    assert service.storage.path(audio).exists()
    assert service.status(reference)["transcript"] == "gigaam / onnx"


def test_sherpa_interruption_retries_locally_and_saved_response_is_reused(voice_database):
    settings, engine = voice_database
    settings = settings.model_copy(update={"asr_beta_enabled": True})
    service = VoiceService(engine, settings)
    reference = service.submit(metadata(), recording(), "audio/wav")["reference"]
    worker = Worker(engine, settings, SixResults())
    assert worker.once() and worker.once()
    interrupted = worker.claim()
    assert interrupted["kind"].startswith("sherpa_")
    # A process can die immediately after claiming, before persisting its request.
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE stages SET lease_until=now()-interval '1 second' WHERE kind=:kind"),
            {"kind": interrupted["kind"]},
        )
    retry = worker.claim()
    assert retry["kind"] == interrupted["kind"] and retry["attempts"] == 2
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE stages SET raw_response=CAST(:response AS jsonb),lease_until=now()-interval '1 second' WHERE kind=:kind"
            ),
            {
                "kind": retry["kind"],
                "response": json.dumps(
                    {"status_code": 200, "body": json.dumps({"text": "Saved Sherpa result"})}
                ),
            },
        )

    class NoCalls:
        def call(self, *args):
            raise AssertionError("Saved raw result must be reused")

    assert Worker(engine, settings, NoCalls()).once()
    row = next(
        r for r in reports(engine, [reference])[reference] if r["text"] == "Saved Sherpa result"
    )
    assert row["variant"].startswith("sherpa_")
    assert service.status(reference)["transcript"] == "gigaam / onnx"
