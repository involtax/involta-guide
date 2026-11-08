import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_guide.infrastructure.voice.client import VoiceClient
from involta_guide.infrastructure.voice.consumer import Consumer
from involta_guide.main import create_app as guide_app
from involta_voice.contracts import LocationContext
from involta_voice.main import create_app as voice_app
from involta_voice.provider import FakeProvider
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


def test_received_then_primary_import_is_automatic_and_replay_safe(database, voice_database):
    settings, engine, _, queries, city, _, _ = setup(database)
    voice_settings, voice_engine = voice_database
    settings = settings.model_copy(update={"voice_service_secret": voice_settings.service_secret})
    data = metadata().model_copy(
        update={
            "location": LocationContext(
                id=city["id"], slug=city["slug"], name=city["name"], content_epoch=1
            )
        }
    )
    service = VoiceService(voice_engine, voice_settings)
    ref = service.submit(data, recording(), "audio/wav")["reference"]
    consumer = Consumer(engine, client_for(settings, voice_settings))
    assert consumer.once() == 1
    assert queries.get("reviews", ref)["transcript"] is None
    from involta_guide.infrastructure.voice.read_model import VoiceReadModel

    read = VoiceReadModel(engine)
    assert read.listing()["total"] == 1
    assert not read.public_texts(city["id"])
    assert read.public_texts(city["id"], visitor=data.visitor)[0]["state"] == "accepted"
    assert Worker(voice_engine, voice_settings, FakeProvider()).once()
    assert consumer.once() == 1
    assert consumer.once() == 0
    assert not read.public_texts(city["id"])
    assert (
        read.public_texts(city["id"], visitor=data.visitor)[0]["transcript"]
        == "Был здесь сегодня, было вкусно."
    )
    assert queries.rows("review_texts") == []
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM review_decisions")).scalar_one() == 0
    assert service.status(ref)["state"] == "delivered_to_guide"


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
        assert (
            client.post(
                "/guide/voice/api/v1/identity?location=ivanovo", headers=headers
            ).status_code
            == 200
        )
        key = str(uuid4())
        payload = {"location": city["slug"], "idempotency_key": key, "actor": "admin"}

        def upload():
            return client.post(
                "/guide/voice/api/v1/submissions?location=ivanovo&content_epoch=1",
                headers=headers,
                data=payload,
                files={"audio": ("x.wav", recording(), "audio/wav")},
            )

        result = upload()
        assert result.status_code == 202, result.text
        reference = result.json()["reference"]
        owner_cookie = client.cookies.get("guide_visitor")
        assert upload().json()["reference"] == reference
        intake_created_at = result.json()["created_at"]
        client.cookies.clear()
        assert client.get("/guide/voice/api/v1/reviews?location=ivanovo").status_code == 200
        assert (
            client.post(
                "/guide/voice/api/v1/submissions", headers={"origin": "https://other.example"}
            ).status_code
            == 403
        )
    with voice_engine.connect() as conn:
        meta = conn.execute(text("SELECT metadata FROM submissions")).scalar_one()
        assert "actor" not in meta
        assert meta["visitor"] == owner_cookie
        assert "attacker" not in json.dumps(meta)
    assert queries.rows("jobs") == []
    worker = Worker(voice_engine, voice_settings, FakeProvider())
    assert worker.once() and not worker.once()
    Consumer(engine, client_for(settings, voice_settings)).once()
    with TestClient(app) as client:
        client.cookies.set("guide_visitor", owner_cookie)
        online = client.get("/guide/voice/api/v1/reviews", params={"location": city["slug"]})
        assert online.json()["rows"][0]["created_at"] == intake_created_at.replace(" ", "T")
    # Imported history remains usable with its location filter when Voice is down.
    from involta_guide.infrastructure.voice.client import VoiceUnavailable

    class Unavailable:
        def json(self, *args, **kwargs):
            raise VoiceUnavailable("Offline")

        def request(self, *args, **kwargs):
            raise VoiceUnavailable("Offline")

    app.state.voice_client = Unavailable()
    with TestClient(app) as client:
        client.cookies.set("guide_visitor", owner_cookie)
        history = client.get("/guide/voice/api/v1/reviews", params={"location": city["slug"]})
        assert history.status_code == 200
        assert history.json()["content_epoch"] == 1
        assert history.json()["rows"][0]["reference"] == reference
        assert history.json()["rows"][0]["transcript"]
        assert (
            history.json()["rows"][0]["created_at"]
            == queries.get("reviews", reference)["created_at"].isoformat()
        )
        app.state.context.settings.voice_enabled = False
        assert (
            client.post(
                "/guide/voice/api/v1/submissions", headers={"origin": "http://testserver"}
            ).status_code
            == 503
        )
        assert client.get("/guide/voice/api/v1/reviews?location=ivanovo").json()["rows"][0][
            "transcript"
        ]


def test_primary_before_receipt_and_legacy_fallback_never_replace_public_text(
    database, voice_database
):
    settings, engine, _, queries, city, _, _ = setup(database)
    voice_settings, voice_engine = voice_database
    settings = settings.model_copy(update={"voice_service_secret": voice_settings.service_secret})
    data = metadata().model_copy(
        update={
            "location": LocationContext(
                id=city["id"], slug=city["slug"], name=city["name"], content_epoch=1
            )
        }
    )
    service = VoiceService(voice_engine, voice_settings)
    ref = service.submit(data, recording(), "audio/wav")["reference"]
    assert Worker(voice_engine, voice_settings, FakeProvider()).once()
    remote = client_for(settings, voice_settings)
    events = remote.json("GET", "/events", params={"after": 0})["events"]
    consumer = Consumer(engine, remote)
    assert consumer.import_event(events[1]) == 1
    assert consumer.import_event(events[0]) == 1
    legacy = {
        **events[1],
        "event_id": str(uuid4()),
        "sequence": events[1]["sequence"] + 1,
        "kind": "transcript_ready",
        "transcript": "Старый fallback Parakeet",
    }
    assert consumer.import_event(legacy) == 1
    assert queries.get("reviews", ref)["transcript"] == "Был здесь сегодня, было вкусно."
    assert queries.get("reviews", ref)["status"] == "transcribed"
    assert len(queries.rows("reviews")) == 1


def test_ack_loss_after_commit_and_concurrent_replay_create_one_review(database, voice_database):
    from concurrent.futures import ThreadPoolExecutor

    settings, engine, _, queries, city, _, _ = setup(database)
    voice_settings, voice_engine = voice_database
    settings = settings.model_copy(update={"voice_service_secret": voice_settings.service_secret})
    meta = metadata().model_copy(
        update={
            "location": LocationContext(
                id=city["id"], slug=city["slug"], name=city["name"], content_epoch=1
            )
        }
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
        assert conn.execute(text("SELECT count(*) FROM voice_inbox")).scalar_one() == 2


def test_ready_primary_is_not_hidden_by_a_received_record_while_consumer_catches_up(
    database, voice_database
):
    settings, engine, _, _, city, _, _ = setup(database)
    voice_settings, voice_engine = voice_database
    settings = settings.model_copy(update={"voice_service_secret": voice_settings.service_secret})
    visitor = str(uuid4())
    data = metadata().model_copy(
        update={
            "visitor": visitor,
            "location": LocationContext(
                id=city["id"], slug=city["slug"], name=city["name"], content_epoch=1
            ),
        }
    )
    VoiceService(voice_engine, voice_settings).submit(data, recording(), "audio/wav")["reference"]
    remote = client_for(settings, voice_settings)
    assert Consumer(engine, remote).once() == 1
    assert Worker(voice_engine, voice_settings, FakeProvider()).once()
    app = guide_app(settings)
    app.state.voice_client = remote
    with TestClient(app) as client:
        client.cookies.set("guide_visitor", visitor)
        pending = client.get("/guide/voice/api/v1/reviews?location=ivanovo").json()["rows"][0]
        assert pending["state"] == "accepted"
        assert Consumer(engine, remote).once() == 1
        assert client.get("/guide/voice/api/v1/reviews?location=ivanovo").json()["rows"][0][
            "transcript"
        ]
    assert Consumer(engine, remote).once() == 0
