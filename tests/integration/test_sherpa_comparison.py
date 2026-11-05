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
from involta_voice.worker import Worker

from .test_jobs import setup
from .test_voice_guide import client_for
from .test_voice_service import metadata, recording
from .test_voice_service import voice_database as voice_database
from .test_web import login


def add_saved_comparison(engine, reference, variant="sherpa_plain"):
    """An already accepted experimental job, never created by today's intake."""
    with engine.begin() as conn:
        snapshot = conn.execute(
            text("SELECT snapshot FROM runs WHERE submission=:id"), {"id": reference}
        ).scalar_one()
        request = dict(snapshot["sherpa_models"]["sherpa_gigaam_hotwords_strong"])
        request.update(variant=variant, hotwords_score=5.0, dictionary=[])
        kind = "sherpa_gigaam_" + variant.removeprefix("sherpa_")
        conn.execute(
            text(
                "INSERT INTO stages(submission,run,kind,request) VALUES(:id,1,:kind,CAST(:request AS jsonb))"
            ),
            {"id": reference, "kind": kind, "request": json.dumps(request)},
        )


class GigaResults(FakeProvider):
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


def test_single_profile_freezes_dictionary_and_freeze_dictionary_with_idempotent_intake(
    voice_database,
):
    settings, engine = voice_database
    settings = settings.model_copy(update={"asr_beta_enabled": True})
    meta = metadata()
    meta.location.recognition_dictionary = ["Алхимист", "Огонёк", "Сыроварня"]
    service = VoiceService(engine, settings)
    audio = recording()
    reference = service.submit(meta, audio, "audio/wav")["reference"]
    assert service.submit(meta, audio, "audio/wav")["reference"] == reference
    queued = reports(engine, [reference])[reference]
    assert len(queued) == 1 and all(r["state"] == "queued" for r in queued)
    assert [r["dictionary"] for r in queued if r["variant"] == "sherpa_hotwords_strong"] == [
        [
            "Алекс Концов",
            "Алхимист",
            "Иванова",
            "Иванове",
            "Иваново",
            "Ивановом",
            "Иванову",
            "Огонёк",
            "Сыроварня",
            "алекс концов",
            "алхимист",
            "иванова",
            "иванове",
            "иваново",
            "ивановом",
            "иванову",
            "огонёк",
            "сыроварня",
        ]
    ]
    meta.location.recognition_dictionary.append("Добавлено позже")
    provider = GigaResults()
    worker = Worker(engine, settings, provider)
    assert all(worker.once() for _ in range(1)) and not worker.once()
    assert len(provider.calls) == 1
    assert {call[3] for call in provider.calls} == {hashlib.sha256(audio).hexdigest()}
    rows = reports(engine, [reference])[reference]
    assert [(r["engine"], r["variant"]) for r in rows] == [("gigaam", "sherpa_hotwords_strong")]
    assert all(r["state"] == "completed" for r in rows)
    assert all(r["dictionary"] == [] for r in rows if r["variant"] == "sherpa_plain")
    assert [(r["hotwords_score"], r["max_active_paths"]) for r in rows] == [(2.5, 16)]
    assert rows[0]["dictionary"] == [
        "Алекс Концов",
        "Алхимист",
        "Иванова",
        "Иванове",
        "Иваново",
        "Ивановом",
        "Иванову",
        "Огонёк",
        "Сыроварня",
        "алекс концов",
        "алхимист",
        "иванова",
        "иванове",
        "иваново",
        "ивановом",
        "иванову",
        "огонёк",
        "сыроварня",
    ]
    assert rows[0]["primary"] and sum(r["primary"] for r in rows) == 1
    assert all(not r["dictionary"] for r in rows if r["variant"] in ("onnx", "sherpa_plain"))
    assert service.status(reference)["transcript"] == "gigaam / sherpa_hotwords_strong"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM submissions")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM asr_candidates")).scalar_one() == 1
        assert (
            conn.execute(text("SELECT count(*) FROM outbox WHERE kind='gigaam_ready'")).scalar_one()
            == 1
        )


@pytest.mark.parametrize("failed", [("gigaam", "sherpa_hotwords_strong")])
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
    worker = Worker(ve, vs, GigaResults(failed))
    assert all(worker.once() for _ in range(1)) and not worker.once()
    assert consumer.once() == 1 and consumer.once() == 0
    rows = reports(ve, [reference])[reference]
    assert sum(r["state"] == "failed" for r in rows) == 1
    failed_row = next(r for r in rows if (r["engine"], r["variant"]) == failed)
    assert failed_row["error"] == "Independent recognition failure"
    stored = queries.get("reviews", reference)
    assert stored["publication_status"] == (
        0 if failed == ("gigaam", "sherpa_hotwords_strong") else 1
    )
    assert stored["transcript"] == (
        None
        if failed == ("gigaam", "sherpa_hotwords_strong")
        else "gigaam / sherpa_hotwords_strong"
    )
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
        conn.execute(text("UPDATE locations SET name='Плёс' WHERE id=:id"), {"id": city["id"]})
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
            assert saved["location"]["recognition_dictionary"] == sorted(["Плёс", *names])
        with engine.begin() as conn:
            conn.execute(text("UPDATE restaurants SET name='Переименован' WHERE name='Алхимист'"))
        worker = Worker(ve, vs, GigaResults())
        assert all(worker.once() for _ in range(1))
        Consumer(engine, client_for(settings, vs)).once()
        login(client)
        page = client.get("/guide/admin/reviews/ivanovo")
        assert page.status_code == 200
        assert page.text.count('class="recognition-dictionary"') == 1
        assert page.text.count("Алхимист") == 1
        assert page.text.count("Плёс · Плёса · Плёсе · Плёсом · Плёсу · Ресторан 000") == 1
        assert "Переименован" not in page.text
        assert page.text.count("Sherpa · словарные подсказки") == 1
        assert "Ресторан 204" in page.text
        public = client.get("/guide/voice/api/v1/reviews?location=ivanovo").json()
        assert public["rows"][0]["transcript"] == "gigaam / sherpa_hotwords_strong"
        assert not any(
            key in public["rows"][0]
            for key in ("variant", "dictionary", "hotwords_score", "max_active_paths")
        )
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
    assert Worker(engine, settings, GigaResults()).once()
    with engine.begin() as conn:
        old = dict(conn.execute(text("SELECT * FROM asr_candidates")).mappings().one())
        old.pop("variant")
        audio = conn.execute(text("SELECT audio FROM submissions")).scalar_one()
        conn.execute(text("DELETE FROM voice_schema_versions WHERE version>=8"))
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
    worker = Worker(engine, settings, GigaResults())
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
    assert service.status(reference)["transcript"] == "Saved Sherpa result"


def test_recommended_profile_publishes_canonical_spelling_but_keeps_raw_results(
    database, voice_database
):
    settings, guide, _, queries, city, _, _ = setup(database)
    vs, ve = voice_database
    vs = vs.model_copy(update={"asr_beta_enabled": True})
    settings = settings.model_copy(update={"voice_service_secret": vs.service_secret})
    meta = metadata().model_copy(
        update={
            "location": LocationContext(
                id=city["id"],
                slug=city["slug"],
                name="Плёс",
                content_epoch=1,
                recognition_dictionary=["Плёс", "Грандъ Сова"],
            )
        }
    )
    service = VoiceService(ve, vs)
    ref = service.submit(meta, recording(), "audio/wav")["reference"]
    raw_text = "Плёс, плюс, плёс равно плёс в квадрате."
    expected = "Плёс, плюс, Плёс равно Плёс в квадрате."

    class ActualText(FakeProvider):
        def call(self, kind, request, audio, mime):
            assert request["variant"] == "sherpa_hotwords_strong"
            assert request["hotwords_score"] == 2.5 and request["max_active_paths"] == 16
            assert request["dictionary"] == [
                "Алекс Концов",
                "Грандъ Сова",
                "Плёс",
                "Плёса",
                "Плёсе",
                "Плёсом",
                "Плёсу",
                "алекс концов",
                "грандъ сова",
                "плёс",
                "плёса",
                "плёсе",
                "плёсом",
                "плёсу",
            ]
            return {"status_code": 200, "body": json.dumps({"text": raw_text})}

    assert Worker(ve, vs, ActualText()).once()
    assert service.status(ref)["transcript"] == expected
    primary = next(r for r in reports(ve, [ref])[ref] if r["primary"])
    assert primary["raw_text"] == raw_text and primary["text"] == expected
    with ve.connect() as conn:
        assert conn.execute(text("SELECT transcript FROM asr_candidates")).scalar_one() == raw_text
        response = conn.execute(text("SELECT response FROM provider_calls")).scalar_one()
        assert json.loads(response["body"])["text"] == raw_text
        assert conn.execute(text("SELECT count(*) FROM stages")).scalar_one() == 1
    assert Consumer(guide, client_for(settings, vs)).once() == 2
    assert queries.get("reviews", ref)["transcript"] == expected


def test_new_primary_failure_and_lease_recovery_are_bound_to_frozen_profile(voice_database):
    from involta_voice.variants import SHERPA_PROFILES

    settings, engine = voice_database
    settings = settings.model_copy(update={"asr_beta_enabled": True})
    service = VoiceService(engine, settings)
    ref = service.submit(metadata(), recording(), "audio/wav")["reference"]
    worker = Worker(engine, settings, GigaResults())
    stage = worker.claim()
    assert stage["kind"] == "sherpa_gigaam_hotwords_strong"
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE stages SET attempts=3,lease_until=now()-interval '1 second' WHERE submission=:id AND kind=:kind"
            ),
            {"id": ref, "kind": stage["kind"]},
        )
    next_stage = worker.claim()
    assert next_stage is None
    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM outbox WHERE kind='gigaam_failed'")
            ).scalar_one()
            == 1
        )
    assert service.status(ref)["state"] == "transcription_failed"
    assert service.status(ref)["transcript"] is None
    assert SHERPA_PROFILES["sherpa_hotwords_strong"] == (2.5, 16, "catalogue")


def test_previously_received_onnx_primary_keeps_its_original_publication_source(voice_database):
    settings, engine = voice_database
    service = VoiceService(engine, settings)
    ref = service.submit(metadata(), recording(), "audio/wav")["reference"]
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE runs SET snapshot=snapshot-'primary_variant'-'restore_dictionary_case' WHERE submission=:id"
            ),
            {"id": ref},
        )
    assert Worker(engine, settings, GigaResults()).once()
    assert service.status(ref)["transcript"] == "gigaam / onnx"
    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT count(*) FROM outbox WHERE kind='gigaam_ready'")).scalar_one()
            == 1
        )


def test_already_received_city_only_job_finishes_without_creating_new_city_jobs(voice_database):
    settings, engine = voice_database
    settings = settings.model_copy(update={"asr_beta_enabled": True})
    service = VoiceService(engine, settings)
    ref = service.submit(metadata(), recording(), "audio/wav")["reference"]
    with engine.begin() as conn:
        frozen = conn.execute(
            text("SELECT snapshot FROM runs WHERE submission=:id"), {"id": ref}
        ).scalar_one()
        request = dict(frozen["sherpa_models"]["sherpa_gigaam_hotwords_strong"])
        request.update(
            variant="sherpa_hotwords_city",
            dictionary=["Плёс"],
            dictionary_encoding="tokens-longest-match-v1",
        )
        conn.execute(
            text(
                "INSERT INTO stages(submission,run,kind,request) VALUES(:id,1,'sherpa_gigaam_hotwords_city',CAST(:request AS jsonb))"
            ),
            {"id": ref, "request": json.dumps(request)},
        )
    worker = Worker(engine, settings, GigaResults())
    for _ in range(2):
        assert worker.once()
    assert not worker.once()
    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT transcript FROM asr_candidates WHERE variant='sherpa_hotwords_city'")
            ).scalar_one()
            == "gigaam / sherpa_hotwords_city"
        )
    assert service.status(ref)["transcript"] == "gigaam / sherpa_hotwords_strong"


def test_form_dictionary_is_frozen_and_inflections_are_published_without_changing_raw(
    voice_database,
):
    settings, engine = voice_database
    settings = settings.model_copy(update={"asr_beta_enabled": True})
    service = VoiceService(engine, settings)
    meta = metadata()
    meta.location.recognition_dictionary = ["Иваново", "Алхимист"]
    ref = service.submit(meta, recording(), "audio/wav")["reference"]
    with engine.connect() as conn:
        frozen = conn.execute(
            text("SELECT snapshot FROM runs WHERE submission=:id"), {"id": ref}
        ).scalar_one()
    assert frozen["name_forms"]["иванове"] == "Иванове"
    assert frozen["name_normalization"] == "catalogue-inflections-v1"
    meta.location.name = "Переименован"

    class Raw(FakeProvider):
        def call(self, *args):
            return {
                "status_code": 200,
                "body": json.dumps({"text": "В иванове есть ивановский ресторан алхимист."}),
            }

    assert Worker(engine, settings, Raw()).once()
    assert service.status(ref)["transcript"] == "В Иванове есть ивановский ресторан Алхимист."
    report = reports(engine, [ref])[ref][0]
    assert report["raw_text"] == "В иванове есть ивановский ресторан алхимист."
    assert report["text"] == service.status(ref)["transcript"]
    assert not Worker(engine, settings, Raw()).once()


@pytest.mark.parametrize("old_run", [False, True])
def test_review_cleanup_is_frozen_preserves_raw_and_does_not_join_city_names(
    voice_database, old_run
):
    from involta_voice.text import REVIEW_TEXT_FORMAT

    settings, engine = voice_database
    settings = settings.model_copy(update={"asr_beta_enabled": True})
    service = VoiceService(engine, settings)
    meta = metadata()
    meta.location.recognition_dictionary = ["Иваново", "Алхимист", "Горы"]
    ref = service.submit(meta, recording(), "audio/wav")["reference"]
    with engine.begin() as conn:
        frozen = conn.execute(
            text("SELECT snapshot FROM runs WHERE submission=:id"), {"id": ref}
        ).scalar_one()
        assert frozen["review_text_format"] == REVIEW_TEXT_FORMAT
        if old_run:
            conn.execute(
                text("UPDATE runs SET snapshot=snapshot-'review_text_format' WHERE submission=:id"),
                {"id": ref},
            )
    raw_text = "Э-э, хочу в алхимист. Также ресторан. горы. Также ресторан. Иваново."

    class Raw(FakeProvider):
        def call(self, *args):
            return {"status_code": 200, "body": json.dumps({"text": raw_text})}

    worker = Worker(engine, settings, Raw())
    assert worker.once() and not worker.once()
    expected = (
        "Э-э, хочу в Алхимист. Также ресторан. Горы. Также ресторан. Иваново."
        if old_run
        else "Хочу в Алхимист. Также ресторан Горы. Также ресторан. Иваново."
    )
    assert service.status(ref)["transcript"] == expected
    report = reports(engine, [ref])[ref][0]
    assert report["raw_text"] == raw_text
    assert report["text"] == expected
    assert report["review_text_format"] == (None if old_run else REVIEW_TEXT_FORMAT)


def test_hesitation_only_recording_keeps_raw_but_does_not_publish_empty_text(voice_database):
    settings, engine = voice_database
    service = VoiceService(engine, settings)
    ref = service.submit(metadata(), recording(), "audio/wav")["reference"]

    class Raw(FakeProvider):
        def call(self, *args):
            return {"status_code": 200, "body": json.dumps({"text": "Э-э, а-а..."})}

    worker = Worker(engine, settings, Raw())
    assert worker.once() and not worker.once()
    status = service.status(ref)
    assert status["state"] == "transcription_failed"
    assert status["transcript"] is None
    with engine.connect() as conn:
        raw = conn.execute(
            text("SELECT raw_response FROM stages WHERE submission=:id"), {"id": ref}
        ).scalar_one()
        assert json.loads(raw["body"])["text"] == "Э-э, а-а..."


def test_accepted_old_primary_retains_five_eight_source_and_exact_normalization(voice_database):
    settings, engine = voice_database
    settings = settings.model_copy(update={"asr_beta_enabled": True})
    service = VoiceService(engine, settings)
    ref = service.submit(metadata(), recording(), "audio/wav")["reference"]
    add_saved_comparison(engine, ref, "sherpa_hotwords")
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE runs SET snapshot=(snapshot-'name_forms'-'name_normalization')||'{\"primary_variant\":\"sherpa_hotwords\"}'::jsonb WHERE submission=:id"
            ),
            {"id": ref},
        )
        before = dict(
            conn.execute(
                text("SELECT request FROM stages WHERE kind='sherpa_gigaam_hotwords'")
            ).scalar_one()
        )

    class Old(FakeProvider):
        def call(self, kind, request, *args):
            assert request == before
            return {"status_code": 200, "body": json.dumps({"text": "в иванове"})}

    assert Worker(engine, settings, Old()).once()
    assert service.status(ref)["transcript"] == "в иванове"
    assert Worker(engine, settings, GigaResults()).once()
    assert service.status(ref)["transcript"] == "в иванове"
    assert len(reports(engine, [ref])[ref]) == 2
