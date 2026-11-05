import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_guide.infrastructure.voice.consumer import Consumer
from involta_guide.infrastructure.voice.read_model import VoiceReadModel
from involta_guide.main import create_app
from involta_voice.contracts import LocationContext, ReprocessRequest
from involta_voice.errors import VoiceError
from involta_voice.provider import FakeProvider, ProviderFailure
from involta_voice.recognition import report, reports
from involta_voice.service import VoiceService
from involta_voice.worker import Worker

from .test_jobs import setup
from .test_voice_guide import client_for
from .test_voice_service import metadata, recording
from .test_voice_service import voice_database as voice_database
from .test_web import login


class Words(FakeProvider):
    def __init__(self, words):
        self.words = words
        self.audio = []

    def call(self, kind, request, audio, mime):
        self.audio.append(audio)
        if self.words is None:
            raise ProviderFailure("Local recognition failed")
        return {"status_code": 200, "body": json.dumps({"text": self.words})}


def test_reprocess_reuses_original_freezes_new_context_and_coalesces_requests(voice_database):
    settings, engine = voice_database
    service = VoiceService(engine, settings)
    meta = metadata()
    audio = recording()
    ref = service.submit(meta, audio, "audio/wav")["reference"]
    assert Worker(engine, settings, Words("Старый текст.")).once()
    request = ReprocessRequest(
        location=meta.location.model_copy(
            update={"recognition_dictionary": ["Горы"], "slug": "new-city-url"}
        ),
        idempotency_key=uuid4(),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.reprocess(ref, request), range(2)))
    assert {r["state"] for r in results} == {"queued", "existing"}
    assert {r["run"] for r in results} == {2}
    another = request.model_copy(update={"idempotency_key": uuid4()})
    assert service.reprocess(ref, another)["state"] == "busy"
    assert service.status(ref)["transcript"] == "Старый текст."
    pair = reports(engine, [ref])[ref][0]
    assert pair["state"] == "queued" and pair["text"] == "Старый текст."
    with engine.connect() as conn:
        frozen = conn.execute(
            text("SELECT snapshot FROM runs WHERE submission=:id AND number=2"), {"id": ref}
        ).scalar_one()
        original_metadata = conn.execute(
            text("SELECT metadata FROM submissions WHERE reference=:id"), {"id": ref}
        ).scalar_one()
    assert frozen["location"]["recognition_dictionary"] == ["Горы"]
    assert frozen["location"]["slug"] == "new-city-url"
    assert original_metadata == meta.model_dump(mode="json")
    fresh = Words("Э-э, новый текст. Также ресторан. горы.")
    assert Worker(engine, settings, fresh).once()
    assert fresh.audio == [audio]
    assert service.status(ref)["transcript"] == "Новый текст. Также ресторан Горы."
    assert service.reprocess(ref, request)["run"] == 2
    assert not Worker(engine, settings, fresh).once()
    history = report(engine, ref)["candidates"]
    assert [r["run"] for r in history] == [2, 1]
    assert history[0]["transcript"] == "Э-э, новый текст. Также ресторан. горы."
    assert history[1]["transcript"] == "Старый текст."


def test_failed_rerun_preserves_published_text_and_allows_another_attempt(voice_database):
    settings, engine = voice_database
    service = VoiceService(engine, settings)
    meta = metadata()
    ref = service.submit(meta, recording(), "audio/wav")["reference"]
    assert Worker(engine, settings, Words("Хороший ресторан.")).once()
    request = ReprocessRequest(location=meta.location, idempotency_key=uuid4())
    assert service.reprocess(ref, request)["run"] == 2
    assert Worker(engine, settings, Words(None)).once()
    assert service.status(ref)["transcript"] == "Хороший ресторан."
    pair = reports(engine, [ref])[ref][0]
    assert pair["state"] == "failed" and pair["error"] == "Local recognition failed"
    assert pair["text"] == "Хороший ресторан."
    assert (
        service.reprocess(ref, request.model_copy(update={"idempotency_key": uuid4()}))["run"] == 3
    )


def test_reprocess_missing_audio_or_wrong_location_does_not_queue(voice_database):
    settings, engine = voice_database
    service = VoiceService(engine, settings)
    meta = metadata()
    ref = service.submit(meta, recording(), "audio/wav")["reference"]
    assert Worker(engine, settings, Words("Текст.")).once()
    request = ReprocessRequest(
        location=meta.location.model_copy(update={"id": 999999}), idempotency_key=uuid4()
    )
    with pytest.raises(VoiceError, match="устарел"):
        service.reprocess(ref, request)
    with engine.connect() as conn:
        filename = conn.execute(
            text("SELECT audio FROM submissions WHERE reference=:id"), {"id": ref}
        ).scalar_one()
    service.storage.path(filename).unlink()
    with pytest.raises(VoiceError, match="аудио недоступно"):
        service.reprocess(ref, request.model_copy(update={"location": meta.location}))
    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM runs WHERE submission=:id"), {"id": ref}
            ).scalar_one()
            == 1
        )


def test_admin_button_current_catalogue_old_event_and_soft_delete_guards(database, voice_database):
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
    service = VoiceService(ve, vs)
    ref = service.submit(meta, recording(), "audio/wav")["reference"]
    assert Worker(ve, vs, Words("Прежний отзыв.")).once()
    remote = client_for(settings, vs)
    consumer = Consumer(engine, remote)
    assert consumer.once() == 2
    before = queries.get("reviews", ref)
    app = create_app(settings)
    app.state.voice_client = remote
    read = VoiceReadModel(engine)
    path = "/guide/admin/reviews/" + city["slug"] + "/" + str(before["number"]) + "/reprocess"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO restaurants(id,location_id,name,cuisine,description,maps_url,active) VALUES(:id,:city,'Горы','','','',0)"
            ),
            {"id": uuid4(), "city": city["id"]},
        )
    with TestClient(app) as client:
        assert client.post(path).status_code == 401
        csrf = login(client)
        assert client.post(path, data={"idempotency_key": str(uuid4())}).status_code == 403
        key = str(uuid4())
        posted = {"_csrf": csrf, "idempotency_key": key}
        response = client.post(path, data=posted, headers={"origin": "http://testserver"})
        assert response.status_code == 200
        assert "Перераспознаётся" in response.text
        assert 'hx-trigger="every 2s"' in response.text
        assert read.public_texts(city["id"])[0]["transcript"] == "Прежний отзыв."
        assert (
            client.post(path, data=posted, headers={"origin": "http://testserver"}).status_code
            == 200
        )
        with ve.connect() as conn:
            frozen = conn.execute(
                text("SELECT snapshot FROM runs WHERE submission=:id AND number=2"), {"id": ref}
            ).scalar_one()
            old_event = conn.execute(
                text(
                    "SELECT payload FROM outbox WHERE submission=:id AND run=1 AND kind='gigaam_ready'"
                ),
                {"id": ref},
            ).scalar_one()
        assert "Горы" in frozen["location"]["recognition_dictionary"]
        assert type(frozen["location"]["id"]) is int
        assert consumer.once() == 1
        assert Worker(ve, vs, Words("Также ресторан. горы.")).once()
        assert consumer.once() == 1
        assert read.public_texts(city["id"])[0]["transcript"] == "Также ресторан Горы."
        after = queries.get("reviews", ref)
        assert (after["id"], after["created_at"], after["number"], after["audio"]) == (
            before["id"],
            before["created_at"],
            before["number"],
            before["audio"],
        )
        old_event["event_id"] = str(uuid4())
        old_event["sequence"] = 1000
        assert consumer._receive(old_event)
        consumer._apply(old_event)
        assert queries.get("reviews", ref)["transcript"] == "Также ресторан Горы."
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE reviews SET status='deleted',publication_status=0 WHERE id=:id"),
                {"id": ref},
            )
        assert (
            client.post(
                path,
                data={"_csrf": csrf, "idempotency_key": str(uuid4())},
                headers={"origin": "http://testserver"},
            ).status_code
            == 404
        )
        assert read.public_texts(city["id"]) == []


@pytest.mark.parametrize("finish", ["failed", "deleted"])
def test_rerun_failure_or_deletion_never_erases_or_resurrects_public_review(
    database, voice_database, finish
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
    service = VoiceService(ve, vs)
    ref = service.submit(meta, recording(), "audio/wav")["reference"]
    worker = Worker(ve, vs, Words("Прежний текст."))
    assert worker.once()
    consumer = Consumer(engine, client_for(settings, vs))
    assert consumer.once() == 2
    service.reprocess(ref, ReprocessRequest(location=meta.location, idempotency_key=uuid4()))
    assert consumer.once() == 1
    if finish == "deleted":
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE reviews SET status='deleted',publication_status=0 WHERE id=:id"),
                {"id": ref},
            )
    assert Worker(ve, vs, Words(None if finish == "failed" else "Новый текст.")).once()
    assert consumer.once() == 1
    review = queries.get("reviews", ref)
    assert review["transcript"] == "Прежний текст."
    assert review["publication_status"] == (1 if finish == "failed" else 0)
    assert review["status"] == ("transcribed" if finish == "failed" else "deleted")


def test_voice_readiness_requires_numeric_location_schema(voice_database):
    from involta_voice.db import migrate
    from involta_voice.main import create_app as create_voice_app

    settings, engine = voice_database
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM voice_schema_versions WHERE version=10"))
    with TestClient(create_voice_app(settings)) as client:
        assert client.get("/health/ready").status_code == 503
        migrate(engine)
        assert client.get("/health/ready").status_code == 200
