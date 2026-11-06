import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_guide.domain.entities import Conflict
from involta_guide.infrastructure.voice.read_model import VoiceReadModel
from involta_guide.main import create_app
from involta_guide.mcp.catalogue import PublicGuide

from .test_reviews2 import RESULT, SOURCE, prepared, state
from .test_web import login, token


@pytest.mark.parametrize("category", ["ok", "humor", "bugs", "other"])
def test_new_visible_then_classification_controls_every_public_feed(database, category):
    settings, engine, city, review, _, _, worker, provider, _ = prepared(
        database, {**RESULT, "category": category}
    )
    read = VoiceReadModel(engine)
    try:
        assert read.public_texts(city["id"])
        assert read.latest_public_texts()
        assert PublicGuide(engine, settings).location(city["slug"]).location.reviews.items
        assert worker.once()
        visible = category in ("ok", "humor")
        assert bool(read.public_texts(city["id"])) == visible
        assert bool(read.latest_public_texts()) == visible
        assert (
            bool(PublicGuide(engine, settings).location(city["slug"]).location.reviews.items)
            == visible
        )
        assert read.publication_states(city["id"], [review])[review] == int(visible)
        assert state(engine, review)["approved_at"] is None
        with TestClient(create_app(settings)) as client:
            assert (SOURCE in client.get("/guide/" + city["slug"]).text) == visible
            assert (SOURCE in client.get("/guide/").text) == visible
            rows = client.get(
                "/guide/voice/api/v1/reviews", params={"location": city["slug"]}
            ).json()["rows"]
            assert bool(rows) == visible
        # Changing the source invalidates automatic classification: it is new again.
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE reviews SET transcript='Новая расшифровка' WHERE id=:id"),
                {"id": review},
            )
        assert read.public_texts(city["id"])
    finally:
        provider.close()


def test_manual_bug_is_audited_and_wins_over_late_ai_and_retranscription(database):
    settings, engine, city, review, _, service, worker, provider, _ = prepared(database)
    try:
        service.schedule()
        service.mark_bug(review, "admin")
        service.mark_bug(review, "admin")
        assert not VoiceReadModel(engine).public_texts(city["id"])
        assert worker.once()  # Existing AI job may finish, but must not override the moderator.
        analysis = state(engine, review)
        assert analysis["result"]["category"] == "ok"
        assert analysis["manual_category"] == "bugs"
        assert not VoiceReadModel(engine).latest_public_texts()
        with pytest.raises(Conflict):
            service.approve(review, str(analysis["job_id"]), "admin")
        with engine.begin() as conn:
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM audit WHERE action='review_marked_bug' AND target=:id"
                    ),
                    {"id": review},
                ).scalar_one()
                == 1
            )
            assert (
                conn.execute(
                    text("SELECT count(*) FROM voice_links WHERE review_id=:id"), {"id": review}
                ).scalar_one()
                == 1
            )
            conn.execute(
                text(
                    "UPDATE reviews SET transcript='Другой текст',publication_status=1 WHERE id=:id"
                ),
                {"id": review},
            )
        assert not VoiceReadModel(engine).public_texts(city["id"])
        with TestClient(create_app(settings)) as client:
            login(client)
            assert "Другой текст" in client.get("/guide/admin/reviews-2/bugs").text
            assert "Другой текст" not in client.get("/guide/admin/reviews-2").text
    finally:
        provider.close()


def test_manual_bug_button_works_before_ai_and_requires_admin_csrf(database):
    settings, engine, city, review, _, _, _, provider, _ = prepared(database)
    try:
        with engine.connect() as conn:
            number = conn.execute(
                text("SELECT number FROM reviews WHERE id=:id"), {"id": review}
            ).scalar_one()
        path = f"/guide/admin/reviews-2/{city['slug']}/{number}/bug"
        with TestClient(create_app(settings)) as client:
            assert client.post(path).status_code == 401
            login(client)
            page = client.get("/guide/admin/reviews-2")
            assert ">Баг</button>" in page.text
            assert client.post(path, headers={"origin": "http://testserver"}).status_code == 403
            response = client.post(
                path,
                data={"_csrf": token(page.text), "status": "new"},
                headers={"origin": "http://testserver"},
                follow_redirects=False,
            )
            assert response.status_code == 303
            assert SOURCE not in client.get("/guide/admin/reviews-2").text
            assert SOURCE in client.get("/guide/admin/reviews-2/bugs").text
            assert not VoiceReadModel(engine).public_texts(city["id"])
    finally:
        provider.close()
