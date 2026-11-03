import hashlib
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_guide.application.services.editorial import EditorialService
from involta_guide.domain.entities import Conflict
from involta_guide.infrastructure.security import Security
from involta_guide.infrastructure.voice.consumer import Consumer
from involta_guide.infrastructure.voice.editorial import Decision, VoiceEditorial
from involta_guide.main import create_app

from .test_editorial import CONTENT
from .test_jobs import setup
from .test_voice_service import metadata, recording
from .test_web import login, token


def imported(database):
    settings, engine, uow, queries, city, _, _ = setup(database)
    original = "Был в Огоньке сегодня, было вкусно. Телефон другой."
    reference = str(uuid4())
    from involta_voice.contracts import LocationContext

    meta = metadata().model_copy(
        update={"location": LocationContext(id=city["id"], slug=city["slug"], name=city["name"])}
    )
    event = {
        "schema_version": 1,
        "event_id": str(uuid4()),
        "sequence": 1,
        "kind": "transcript_ready",
        "reference": reference,
        "run": 1,
        "metadata": meta.model_dump(mode="json"),
        "transcript": original,
        "analysis": None,
        "sha256": hashlib.sha256(recording()).hexdigest(),
        "mime": "audio/wav",
        "duration": 2,
        "created_at": "2026-11-03T00:00:00Z",
    }
    from involta_guide.infrastructure.voice.client import VoiceClient

    Consumer(engine, VoiceClient(settings)).import_event(event)
    return settings, engine, uow, queries, city, reference, event


def test_admin_ui_moderates_text_without_analysis_and_guards_stale_screen(database):
    settings, engine, uow, queries, city, ref, event = imported(database)
    with TestClient(create_app(settings)) as client:
        assert client.get("/guide/admin/voice").status_code == 401
        login(client)
        page = client.get("/guide/admin/voice/ivanovo/1")
        assert page.status_code == 200, page.text
        assert event["transcript"] in page.text
        assert "Предложений нет" in page.text
        assert "Сохранить текст" in page.text
        csrf = token(page.text)

        def decide(action, revision, **values):
            return client.post(
                "/guide/admin/voice/decision/" + ref,
                data={
                    "_csrf": csrf,
                    "idempotency_key": str(uuid4()),
                    "processing_run": "1",
                    "review_revision": str(revision),
                    "action": action,
                    **values,
                },
                headers={"origin": "http://testserver"},
            )

        assert decide("edit-text", 1, text="Редакторская пунктуация.").status_code == 200
        assert decide("approve", 1).status_code == 409
        assert decide("approve", 2).status_code == 200
        assert "Редакторская пунктуация." in client.get("/guide/ivanovo").text
        assert decide("reject", 3).status_code == 200
        assert "Редакторская пунктуация." not in client.get("/guide/ivanovo").text
        assert client.get("/guide/logs/" + ref).status_code == 404
    assert queries.get("review_texts", ref)["original_text"] == event["transcript"]
    assert queries.get("reviews", ref)["moderation"] == "rejected"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM review_decisions")).scalar_one() == 3


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("repair", [False, True])
def test_package_atomic_rollback_exclusion_conflict_and_idempotent_immediate_application(
    database, repair, shared
):
    settings, engine, uow, queries, city, ref, event = imported(database)
    editor = EditorialService(uow)
    version = editor.save(city["id"], CONTENT, None)
    editor.publish(city["id"], version, None)
    card = queries.location(city["id"])["content"]["restaurants"][0]
    affected = {}
    other = None
    if shared:
        other = queries.directory(query="Шуя")["rows"][0]
        with uow() as tx:
            tx.repo.link(
                "restaurant_locations", {"restaurant_id": card["id"], "location_id": other["id"]}
            )
        other_version = editor.save(
            other["id"], {"intro": "Shared card", "restaurants": [card]}, None
        )
        affected[other["id"]] = other_version

    from involta_voice.contracts import RestaurantContext

    event["metadata"]["location"]["restaurants"] = [
        RestaurantContext(id=card["id"], name=card["name"]).model_dump(mode="json")
    ]
    event.update(
        event_id=str(uuid4()),
        sequence=2,
        kind="analysis_ready",
        analysis={
            "outcome": "review",
            "entries": [],
            "suggestions": [
                {
                    "kind": "edit",
                    "restaurantId": card["id"],
                    "quote": "Телефон другой.",
                    "reason": "Проверить телефон",
                    "changes": {"phone": "+74951234568"},
                },
                {
                    "kind": "restaurant",
                    "name": "Новое место",
                    "quote": "Был в Огоньке",
                    "reason": "Проверить новое место",
                    "changes": {"phone": None},
                },
            ],
        },
    )
    from involta_guide.infrastructure.voice.client import VoiceClient

    Consumer(engine, VoiceClient(settings)).import_event(event)
    principal = Security(settings, uow).principal(
        Security(settings, uow).login("test-password-123456", "test")
    )
    service = VoiceEditorial(engine)
    proposals = queries.rows("proposals", {"review_id": ref})
    command = Decision(
        idempotency_key=uuid4(),
        review_revision=2,
        processing_run=1,
        action="approve",
        page_revision=version,
        affected_revisions=affected,
    )
    from pydantic import ValidationError

    with pytest.raises((ValidationError, ValueError)):
        service.decide(ref, command, principal["id"])
    assert queries.get("locations", city["id"])["published_version"] == version
    assert all(p["status"] == "pending" for p in queries.rows("proposals"))
    assert queries.get("reviews", ref)["moderation"] == "pending"
    invalid = next(p for p in proposals if p["kind"] == "restaurant")
    service.decide(
        ref,
        Decision(
            idempotency_key=uuid4(),
            review_revision=2,
            processing_run=1,
            action="edit-proposal" if repair else "exclude-proposal",
            changes={
                **{
                    k: v
                    for k, v in CONTENT["restaurants"][0].items()
                    if k in {"name", "cuisine", "description", "mapsUrl", "phone", "openingHours"}
                },
                "name": "Новое место",
                "phone": "+74951234569",
            }
            if repair
            else {},
            proposal_id=invalid["id"],
            proposal_revision=1,
        ),
        principal["id"],
    )
    with pytest.raises(Conflict):
        service.decide(ref, command, principal["id"])
    newer = editor.save(city["id"], queries.location(city["id"])["content"], version)
    command = command.model_copy(update={"review_revision": 3})
    with pytest.raises(Conflict):
        service.decide(ref, command, principal["id"])
    command = command.model_copy(update={"page_revision": __import__("uuid").UUID(newer)})
    result = service.decide(ref, command, principal["id"])
    assert service.decide(ref, command, principal["id"]) == result
    assert len(queries.location(city["id"])["content"]["restaurants"]) == (2 if repair else 1)
    if other:
        assert queries.get("locations", other["id"])["published_version"] != other_version
        assert (
            queries.get("versions", other_version)["content"]["restaurants"][0]["phone"]
            != "+74951234568"
        )
        assert queries.location(other["id"])["content"]["restaurants"][0]["phone"] == "+74951234568"

    assert queries.get("locations", city["id"])["published_version"] != newer
    assert queries.location(city["id"])["content"]["restaurants"][0]["phone"] == "+74951234568"
    assert queries.get("review_texts", ref)["original_text"] == event["transcript"]
    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM review_decisions WHERE action='approve'")
            ).scalar_one()
            == 1
        )


def test_admin_cookie_marks_trusted_voice_and_shortcut_sets_same_admin_cookie(database):
    settings, engine, uow, queries, city, _, _ = setup(database)
    with TestClient(create_app(settings)) as client:
        csrf = login(client)
        response = client.post(
            "/guide/api/reviews",
            data={"locationId": city["id"], "_csrf": csrf},
            files={"audio": ("recording.wav", recording(), "audio/wav")},
            headers={"origin": "http://testserver"},
        )
        assert response.status_code == 201, response.text
        review = queries.get("reviews", response.json()["id"])
        assert review["admin_session_id"] is not None and review["alex_session_id"] is None
        assert client.get("/guide/ivanovo").text.count("Режим Алекса:") == 1
        assert (
            client.post(
                "/guide/api/reviews",
                data={"locationId": city["id"]},
                files={"audio": ("x.wav", recording(), "audio/wav")},
                headers={"origin": "http://testserver"},
            ).status_code
            == 403
        )


def test_consumer_diagnostics_blocked_event_and_recovery(database, caplog):
    settings, engine, _, _, _, ref, event = imported(database)
    from involta_guide.infrastructure.voice.client import VoiceUnavailable
    from involta_guide.infrastructure.voice.consumer import Consumer
    from involta_guide.infrastructure.voice.read_model import VoiceReadModel

    event = {**event, "sequence": 2, "event_id": str(uuid4())}

    class Source:
        mode = "outage"

        def json(self, method, path, **kwargs):
            if self.mode == "outage":
                raise VoiceUnavailable("Private diagnostic must not appear in logs")
            if path == "/events/ack":
                return {"acknowledged": True}
            return {"events": [{**event, "transcript": ""} if self.mode == "invalid" else event]}

    source = Source()
    consumer = Consumer(engine, source)
    with pytest.raises(VoiceUnavailable):
        consumer.once()
    assert VoiceReadModel(engine).consumer_health()["error_type"] == "VoiceUnavailable"
    source.mode = "invalid"
    with pytest.raises(ValueError):
        consumer.once()
    diagnostic = VoiceReadModel(engine).consumer_health()
    assert diagnostic["sequence"] == 2 and not diagnostic["healthy"]
    assert "Private diagnostic" not in caplog.text
    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT sequence FROM voice_cursor WHERE consumer='voice'")
            ).scalar_one()
            == 1
        )
    source.mode = "valid"
    assert consumer.once() == 1
    assert VoiceReadModel(engine).consumer_health()["healthy"]
    assert consumer.once() == 0
