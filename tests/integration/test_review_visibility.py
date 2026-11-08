import json

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
def test_only_author_sees_pending_then_classification_controls_every_public_feed(
    database, category
):
    settings, engine, city, review, event, _, worker, provider, _ = prepared(
        database, {**RESULT, "category": category}
    )
    read = VoiceReadModel(engine)
    owner = event["metadata"]["visitor"]
    try:
        assert not read.public_texts(city["id"])
        assert not read.latest_public_texts()
        assert not PublicGuide(engine, settings).location(city["slug"]).location.reviews.items
        assert read.public_texts(city["id"], visitor=owner)[0]["transcript"] == SOURCE
        assert read.publication_states(city["id"], [review], visitor=owner)[review] == 1
        assert read.publication_states(city["id"], [review])[review] == 0
        with TestClient(create_app(settings)) as client:
            assert SOURCE not in client.get("/guide/" + city["slug"]).text
            client.cookies.set("guide_visitor", owner)
            assert SOURCE in client.get("/guide/" + city["slug"]).text
            response = client.get(
                "/guide/voice/api/v1/reviews",
                params={"location": city["slug"], "pending": review},
            ).json()
            assert response["rows"][0]["transcript"] == SOURCE
            assert response["publication_statuses"][review] == 1
            # Even the author's cookie does not personalize the home feed.
            assert SOURCE not in client.get("/guide/").text
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
        assert not read.public_texts(city["id"])
        assert read.public_texts(city["id"], visitor=owner)[0]["transcript"] == "Новая расшифровка"
    finally:
        provider.close()


@pytest.mark.parametrize("job_status", ["queued", "running", "failed", "cancelled"])
def test_unfinished_or_failed_classification_stays_private_to_author(database, job_status):
    settings, engine, city, review, event, _, worker, provider, _ = prepared(database)
    try:
        assert worker.once()
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE jobs SET status=:status WHERE id=:id"),
                {"id": state(engine, review)["job_id"], "status": job_status},
            )
        read = VoiceReadModel(engine)
        owner = event["metadata"]["visitor"]
        assert read.public_texts(city["id"], visitor=owner)[0]["transcript"] == SOURCE
        assert not read.public_texts(city["id"], visitor="another-browser")
        assert not read.latest_public_texts()
        assert not PublicGuide(engine, settings).location(city["slug"]).location.reviews.items
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


@pytest.mark.parametrize("approved", [False, True])
def test_manual_humor_from_new_or_ok_keeps_text_and_reverses_votes(database, approved):
    settings, engine, city, review, _, service, worker, provider, _ = prepared(database)
    try:
        assert worker.once()
        job = str(state(engine, review)["job_id"])
        if approved:
            service.approve(review, job, "admin")
        with engine.connect() as conn:
            number = conn.execute(
                text("SELECT number FROM reviews WHERE id=:id"), {"id": review}
            ).scalar_one()
            before = conn.execute(
                text("SELECT transcript,edited_text FROM reviews WHERE id=:id"), {"id": review}
            ).one()
        path = f"/guide/admin/reviews-2/{city['slug']}/{number}/humor"
        with TestClient(create_app(settings)) as client:
            assert client.post(path).status_code == 401
            login(client)
            tab = "ok" if approved else "new"
            page = client.get("/guide/admin/reviews-2/" + tab)
            assert ">Юмор</button>" in page.text
            assert client.post(path, headers={"origin": "http://testserver"}).status_code == 403
            data = {"_csrf": token(page.text), "status": tab}
            for _ in range(2):
                assert (
                    client.post(
                        path, data=data, headers={"origin": "http://testserver"}
                    ).status_code
                    == 200
                )
            assert f'id="review-{number}"' not in client.get("/guide/admin/reviews-2/" + tab).text
            humor = client.get("/guide/admin/reviews-2/humor").text
            assert SOURCE in humor and "Вручную: Юмор" in humor
        assert VoiceReadModel(engine).public_texts(city["id"])
        assert VoiceReadModel(engine).latest_public_texts()
        assert PublicGuide(engine, settings).location(city["slug"]).location.reviews.items
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT transcript,edited_text FROM reviews WHERE id=:id"), {"id": review}
                ).one()
                == before
            )
            assert (
                conn.execute(text("SELECT count(*) FROM review_restaurant_votes")).scalar_one() == 0
            )
            assert (
                conn.execute(
                    text("SELECT coalesce(sum(review_score),0) FROM restaurants")
                ).scalar_one()
                == 0
            )
            audit = (
                conn.execute(
                    text(
                        "SELECT detail FROM audit WHERE action='review_marked_humor' AND target=:id"
                    ),
                    {"id": review},
                )
                .scalars()
                .all()
            )
            assert len(audit) == 1
            detail = json.loads(audit[0])
            assert detail["actor"] == str(
                conn.execute(text("SELECT id FROM admin_sessions")).scalar_one()
            )
            assert len(detail["removed_votes"]) == int(approved)
        with pytest.raises(Conflict):
            service.approve(review, job, "admin")
    finally:
        provider.close()


def test_manual_humor_overrides_late_other_and_retranscription(database):
    settings, engine, city, review, _, service, worker, provider, _ = prepared(
        database, {**RESULT, "category": "other"}
    )
    try:
        service.schedule()
        service.mark_humor(review, "admin")
        assert worker.once()
        assert state(engine, review)["result"]["category"] == "other"
        assert VoiceReadModel(engine).public_texts(city["id"])
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE reviews SET transcript='Харчо друга' WHERE id=:id"), {"id": review}
            )
        assert service.schedule() == 0
        assert VoiceReadModel(engine).public_texts(city["id"])
        with TestClient(create_app(settings)) as client:
            login(client)
            assert "Харчо друга" in client.get("/guide/admin/reviews-2/humor").text
            assert "Харчо друга" not in client.get("/guide/admin/reviews-2").text
    finally:
        provider.close()


@pytest.mark.parametrize(
    "category,hidden_by",
    [
        ("other", None),
        ("bugs", None),
        ("other", "deleted"),
        ("other", "manual_bug"),
    ],
)
def test_only_author_sees_other_on_location_and_widget(database, category, hidden_by):
    from uuid import uuid4

    settings, engine, city, review, event, service, worker, provider, _ = prepared(
        database, {**RESULT, "category": category}
    )
    try:
        assert worker.once()
        if hidden_by == "deleted":
            service.delete(review, "admin")
        elif hidden_by == "manual_bug":
            service.mark_bug(review, "admin")
        owner = event["metadata"]["visitor"]
        for visitor in (owner, str(uuid4()), None):
            visible = visitor == owner and category == "other" and hidden_by is None
            with TestClient(create_app(settings)) as client:
                if visitor:
                    client.cookies.set("guide_visitor", visitor)
                page = client.get("/guide/" + city["slug"])
                assert (SOURCE in page.text) == visible
                assert "no-store" in page.headers["cache-control"]
                assert "cookie" in page.headers["vary"].lower()
                response = client.get(
                    "/guide/voice/api/v1/reviews",
                    params={"location": city["slug"], "pending": review},
                )
                assert response.status_code == 200
                assert "no-store" in response.headers["cache-control"]
                data = response.json()
                assert bool(data["rows"]) == visible
                assert data["publication_statuses"][review] == int(visible)
                if visible:
                    assert data["rows"][0]["transcript"] == SOURCE
                    assert SOURCE in client.get("/guide/" + city["slug"]).text
                assert SOURCE not in client.get("/guide/").text
        read = VoiceReadModel(engine)
        assert not read.public_texts(city["id"])
        assert not read.latest_public_texts()
        assert not PublicGuide(engine, settings).location(city["slug"]).location.reviews.items
    finally:
        provider.close()
