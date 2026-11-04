import hashlib
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_guide.domain.entities import Conflict
from involta_guide.infrastructure.security import Security
from involta_guide.infrastructure.voice.consumer import Consumer
from involta_guide.infrastructure.voice.editorial import Decision, VoiceEditorial
from involta_guide.infrastructure.voice.read_model import VoiceReadModel
from involta_guide.main import create_app

from .editor_fixture import FixtureEditor as EditorialService
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
        update={
            "location": LocationContext(
                id=city["id"], slug=city["slug"], name=city["name"], content_epoch=1
            )
        }
    )
    event = {
        "schema_version": 2,
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
    path = "/guide" + VoiceReadModel(engine).path(ref)
    with TestClient(create_app(settings)) as client:
        assert client.get("/guide/admin/reviews").status_code == 401
        login(client)
        page = client.get(path)
        assert page.status_code == 200, page.text
        assert event["transcript"] in page.text
        assert "Предложений нет" in page.text
        assert "Сохранить текст" in page.text
        csrf = token(page.text)

        def decide(action, revision, **values):
            return client.post(
                path + "/actions",
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


@pytest.mark.parametrize("repair", [False, True])
def test_package_atomic_rollback_exclusion_conflict_and_idempotent_immediate_application(
    database, repair
):
    settings, engine, uow, queries, city, ref, event = imported(database)
    editor = EditorialService(uow)
    version = editor.save(city["id"], CONTENT, None)
    card = queries.location(city["id"])["content"]["restaurants"][0]
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
    )
    from pydantic import ValidationError

    with pytest.raises((ValidationError, ValueError)):
        service.decide(ref, command, principal["id"])
    assert queries.get("locations", city["id"])["revision"] == version
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
    newer = editor.save(
        city["id"],
        {**queries.location(city["id"])["content"], "intro": "Конкурентная правка"},
        version,
    )
    command = command.model_copy(update={"review_revision": 3})
    with pytest.raises(Conflict):
        service.decide(ref, command, principal["id"])
    command = command.model_copy(update={"page_revision": __import__("uuid").UUID(newer)})
    result = service.decide(ref, command, principal["id"])
    assert service.decide(ref, command, principal["id"]) == result
    assert len(queries.location(city["id"])["content"]["restaurants"]) == (2 if repair else 1)

    assert queries.get("locations", city["id"])["revision"] != newer
    assert queries.location(city["id"])["content"]["restaurants"][0]["phone"] == "+74951234568"
    assert queries.get("review_texts", ref)["original_text"] == event["transcript"]
    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM review_decisions WHERE action='approve'")
            ).scalar_one()
            == 1
        )
