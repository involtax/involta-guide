import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_guide.domain.entities import Conflict
from involta_guide.infrastructure.security import Security
from involta_guide.infrastructure.voice.client import VoiceClient
from involta_guide.infrastructure.voice.consumer import Consumer
from involta_guide.infrastructure.voice.editorial import Decision, VoiceEditorial
from involta_guide.main import create_app as guide_app
from involta_voice.contracts import LocationContext
from involta_voice.main import create_app as voice_app
from involta_voice.provider import FakeProvider, ProviderFailure
from involta_voice.service import VoiceService
from involta_voice.worker import Worker

from .test_jobs import setup
from .test_voice_service import metadata, recording
from .test_voice_service import voice_database as voice_database


def client_for(guide_settings, voice_settings):
    remote = TestClient(voice_app(voice_settings))

    def handle(request):
        response = remote.request(
            request.method, str(request.url), content=request.content, headers=request.headers
        )
        return httpx.Response(
            response.status_code, content=response.content, headers=response.headers
        )

    return VoiceClient(guide_settings, httpx.MockTransport(handle))


def test_text_import_survives_analysis_failure_replay_and_rejection(database, voice_database):
    settings, engine, uow, queries, city, _, _ = setup(database)
    voice_settings, voice_engine = voice_database
    settings = settings.model_copy(update={"voice_service_secret": voice_settings.service_secret})
    data = metadata().model_copy(
        update={"location": LocationContext(id=city["id"], slug=city["slug"], name=city["name"])}
    )
    service = VoiceService(voice_engine, voice_settings)
    reference = service.submit(data, recording(), "audio/wav")["reference"]

    class Failure(FakeProvider):
        def call(self, kind, *args):
            if kind == "analysis":
                raise ProviderFailure("OpenAI HTTP 403")
            return super().call(kind, *args)

    assert Worker(voice_engine, voice_settings, Failure()).once()
    consumer = Consumer(engine, client_for(settings, voice_settings))
    assert consumer.once() == 1
    assert Worker(voice_engine, voice_settings, Failure()).once()
    stored = queries.get("review_texts", reference)
    assert stored["original_text"] == stored["editorial_text"] == "Был здесь сегодня, было вкусно."
    assert queries.get("reviews", reference)["moderation"] == "pending"
    assert queries.get("reviews", reference)["result"]["entries"] == []
    assert consumer.once() == 1
    assert consumer.once() == 0
    with voice_engine.connect() as conn:
        event = (
            conn.execute(text("SELECT sequence,payload FROM outbox WHERE kind='transcript_ready'"))
            .mappings()
            .one()
        )
    assert consumer.import_event({"sequence": event["sequence"], **event["payload"]}) == 0
    session = Security(settings, uow).login("test-password-123456", "test-ip")
    principal = Security(settings, uow).principal(session)
    command = Decision(
        idempotency_key=uuid4(), review_revision=1, processing_run=1, action="reject"
    )
    editorial = VoiceEditorial(engine)
    result = editorial.decide(reference, command, principal["id"])
    assert editorial.decide(reference, command, principal["id"]) == result
    assert queries.get("reviews", reference)["moderation"] == "rejected"
    assert queries.get("review_texts", reference)["original_text"] == stored["original_text"]
    assert service.storage.path(
        next((voice_settings.data_dir / "audio").glob("*.wav")).name
    ).exists()
    with pytest.raises(Conflict):
        editorial.decide(
            reference,
            command.model_copy(update={"idempotency_key": uuid4(), "action": "approve"}),
            principal["id"],
        )
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM review_decisions")).scalar_one() == 1


def test_gateway_rewrites_actor_and_ownership_before_lost_202_retry(database, voice_database):
    settings, engine, _, queries, city, _, _ = setup(database)
    voice_settings, voice_engine = voice_database
    settings = settings.model_copy(
        update={"voice_service_secret": voice_settings.service_secret, "voice_enabled": True}
    )
    app = guide_app(settings)
    app.state.voice_client = client_for(settings, voice_settings)
    with TestClient(app) as client:
        headers = {"origin": "http://testserver", "role": "admin", "x-forwarded-for": "attacker"}
        assert client.post("/guide/voice/api/v1/identity", headers=headers).status_code == 200
        key = str(uuid4())
        payload = {"location": city["slug"], "idempotency_key": key, "actor": "admin"}

        def upload():
            return client.post(
                "/guide/voice/api/v1/submissions",
                headers=headers,
                data=payload,
                files={"audio": ("x.wav", recording(), "audio/wav")},
            )

        result = upload()
        assert result.status_code == 202, result.text
        reference = result.json()["reference"]
        assert upload().json()["reference"] == reference
        assert client.get("/guide/voice/api/v1/submissions/" + reference).status_code == 200
        client.cookies.clear()
        assert client.get("/guide/voice/api/v1/submissions/" + reference).status_code == 404
        assert client.post("/guide/voice/api/v1/identity", headers=headers).status_code == 200
        assert client.get("/guide/voice/api/v1/submissions/" + reference).status_code == 404
        assert (
            client.post(
                "/guide/voice/api/v1/submissions", headers={"origin": "https://other.example"}
            ).status_code
            == 403
        )
    with voice_engine.connect() as conn:
        meta = conn.execute(text("SELECT metadata FROM submissions")).scalar_one()
        assert meta["actor"] == "visitor"
        assert "attacker" not in json.dumps(meta)
    assert queries.rows("jobs") == []


def test_out_of_order_analysis_creates_text_once_and_new_runs_do_not_undo_moderation(
    database, voice_database
):
    settings, engine, uow, queries, city, _, _ = setup(database)
    voice_settings, voice_engine = voice_database
    settings = settings.model_copy(update={"voice_service_secret": voice_settings.service_secret})
    meta = metadata().model_copy(
        update={"location": LocationContext(id=city["id"], slug=city["slug"], name=city["name"])}
    )
    service = VoiceService(voice_engine, voice_settings)
    ref = service.submit(meta, recording(), "audio/wav")["reference"]
    worker = Worker(voice_engine, voice_settings, FakeProvider())
    assert worker.once() and worker.once()
    with voice_engine.connect() as conn:
        events = [
            {"sequence": r["sequence"], **r["payload"]}
            for r in conn.execute(text("SELECT * FROM outbox ORDER BY sequence")).mappings()
        ]
    consumer = Consumer(engine, client_for(settings, voice_settings))
    assert consumer.import_event(events[1]) == 1
    assert consumer.import_event(events[0]) == 1
    assert len(queries.rows("reviews")) == 1
    principal = Security(settings, uow).principal(
        Security(settings, uow).login("test-password-123456", "test")
    )
    VoiceEditorial(engine).decide(
        ref,
        Decision(idempotency_key=uuid4(), review_revision=1, processing_run=1, action="approve"),
        principal["id"],
    )
    service.new_run(ref)
    assert worker.once()
    assert consumer.once() == 1
    assert queries.get("reviews", ref)["moderation"] == "approved"
    assert queries.get("voice_links", ref)["active_run"] == 1
    assert queries.get("review_texts", ref)["revision"] == 2


def test_ack_loss_after_commit_and_concurrent_replay_create_one_review(database, voice_database):
    from concurrent.futures import ThreadPoolExecutor

    settings, engine, _, queries, city, _, _ = setup(database)
    voice_settings, voice_engine = voice_database
    settings = settings.model_copy(update={"voice_service_secret": voice_settings.service_secret})
    meta = metadata().model_copy(
        update={"location": LocationContext(id=city["id"], slug=city["slug"], name=city["name"])}
    )
    VoiceService(voice_engine, voice_settings).submit(meta, recording(), "audio/wav")
    assert Worker(voice_engine, voice_settings, FakeProvider()).once()
    remote = client_for(settings, voice_settings)

    class LoseAck:
        def json(self, method, path, **kwargs):
            if path == "/events/ack":
                raise RuntimeError("Simulated lost acknowledgement")
            return remote.json(method, path, **kwargs)

    consumer = Consumer(engine, LoseAck())
    with pytest.raises(RuntimeError, match="lost acknowledgement"):
        consumer.once()
    assert len(queries.rows("reviews")) == 1
    assert Consumer(engine, remote).once() == 0
    event = remote.json("GET", "/events", params={"after": 0})["events"][0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(Consumer(engine, remote).import_event, [event, event]))
    assert results == [0, 0]
    assert len(queries.rows("reviews")) == 1
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM voice_inbox")).scalar_one() == 1
