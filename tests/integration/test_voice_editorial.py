import hashlib
from uuid import UUID, uuid4

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
        assert "Редакторская пунктуация." not in client.get("/guide/ivanovo").text
        assert decide("publish-text", 3).status_code == 200
        assert "Редакторская пунктуация." in client.get("/guide/ivanovo").text
        assert decide("reject", 4).status_code == 200
        assert "Редакторская пунктуация." not in client.get("/guide/ivanovo").text
        assert client.get("/guide/logs/" + ref).status_code == 404
    assert queries.get("review_texts", ref)["original_text"] == event["transcript"]
    assert queries.get("reviews", ref)["moderation"] == "rejected"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM review_decisions")).scalar_one() == 4


def test_proposal_partial_acceptance_detects_page_conflict_and_preserves_other_actions(database):
    settings, engine, uow, queries, city, ref, event = imported(database)
    editor = EditorialService(uow)
    version = editor.save(city["id"], CONTENT, None)
    editor.publish(city["id"], version, None)
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
    service.decide(
        ref,
        Decision(idempotency_key=uuid4(), review_revision=1, processing_run=1, action="approve"),
        principal["id"],
    )
    proposals = queries.rows("proposals", {"review_id": ref})
    proposal = next(p for p in proposals if p["kind"] == "edit")
    stale = Decision(
        idempotency_key=uuid4(),
        review_revision=2,
        processing_run=1,
        action="approve-proposal",
        proposal_id=proposal["id"],
        proposal_revision=1,
        page_revision=version,
        changes={"phone": "+74951234568"},
    )
    newer = editor.save(city["id"], queries.location(city["id"])["content"], version)
    with pytest.raises(Conflict):
        service.decide(ref, stale, principal["id"])
    assert queries.get("proposals", proposal["id"])["status"] == "pending"
    result = service.decide(
        ref,
        stale.model_copy(update={"idempotency_key": uuid4(), "page_revision": UUID(newer)}),
        principal["id"],
    )
    assert result["version_id"] != version
    assert queries.get("locations", city["id"])["published_version"] == newer
    assert queries.location(city["id"])["content"]["restaurants"][0]["phone"] != "+74951234568"
    assert (
        queries.get("versions", result["version_id"])["content"]["restaurants"][0]["phone"]
        == "+74951234568"
    )
    published = service.decide(
        ref,
        Decision(
            idempotency_key=uuid4(),
            review_revision=3,
            processing_run=1,
            action="publish-page",
            page_revision=result["version_id"],
            version_id=result["version_id"],
            published_revision=newer,
        ),
        principal["id"],
    )
    assert (
        queries.get("locations", city["id"])["published_version"] == published["published_version"]
    )
    assert queries.location(city["id"])["content"]["restaurants"][0]["phone"] == "+74951234568"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM voice_page_drafts")).scalar_one() == 0
        assert (
            conn.execute(
                text("SELECT count(*) FROM review_decisions WHERE action='publish-page'")
            ).scalar_one()
            == 1
        )
    assert (
        next(p for p in queries.rows("proposals") if p["kind"] == "restaurant")["status"]
        == "pending"
    )
    assert queries.get("review_texts", ref)["original_text"] == event["transcript"]
    from starlette.requests import Request

    from involta_guide.web.routes.voice import editorial_status

    app = create_app(settings)
    request = Request({"type": "http", "app": app, "headers": []})
    try:
        assert (
            editorial_status(request, ref, event["metadata"]["visitor"])["editorial_state"]
            == "published"
        )
        service.decide(
            ref,
            Decision(idempotency_key=uuid4(), review_revision=4, processing_run=1, action="reject"),
            principal["id"],
        )
        assert (
            editorial_status(request, ref, event["metadata"]["visitor"])["editorial_state"]
            == "rejected"
        )
    finally:
        app.state.context.engine.dispose()


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
