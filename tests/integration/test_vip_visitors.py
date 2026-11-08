from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from involta_guide.infrastructure.persistence.release_backup import owned_tables
from involta_guide.infrastructure.voice.consumer import Consumer
from involta_guide.infrastructure.voice.read_model import VoiceReadModel
from involta_guide.main import create_app
from involta_guide.mcp.catalogue import PublicGuide

from .test_reviews2 import RESULT, prepared, state
from .test_web import login, token

WATCHED = {
    **RESULT,
    "category": "watched",
    "author_name": None,
    "edited_text": "Я всё досмотрел.",
    "mentions": [],
}


def vip_rows(engine):
    with engine.connect() as conn:
        return list(conn.execute(text("SELECT visitor_id,level FROM vip_visitors")).all())


def watched(database):
    context = prepared(database, WATCHED)
    _, engine, _, review, *_ = context
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE reviews SET transcript=:source WHERE id=:id"),
            {"source": WATCHED["edited_text"], "id": review},
        )
    return context


def test_classification_grants_only_author_once_and_does_not_publish_or_vote(database):
    settings, engine, city, review, event, service, worker, provider, calls = watched(database)
    try:
        assert vip_rows(engine) == []
        assert worker.once()
        owner = event["metadata"]["visitor"]
        assert vip_rows(engine) == [(owner, 1)]
        assert state(engine, review)["approved_at"] is None
        read = VoiceReadModel(engine)
        assert not read.public_texts(city["id"])
        assert (
            read.public_texts(city["id"], visitor=owner)[0]["transcript"] == WATCHED["edited_text"]
        )
        assert not read.latest_public_texts()
        assert not PublicGuide(engine, settings).location(city["slug"]).location.reviews.items
        assert service.schedule() == 0
        assert not worker.once()
        assert len(calls) == 1 and vip_rows(engine) == [(owner, 1)]
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT count(*) FROM review_restaurant_votes")).scalar_one() == 0
            )
            assert (
                conn.execute(
                    text("SELECT count(*) FROM restaurants WHERE name='Листва'")
                ).scalar_one()
                == 0
            )
    finally:
        provider.close()


@pytest.mark.parametrize("category", ["ok", "humor", "bugs", "other"])
def test_other_categories_never_grant_vip(database, category):
    _, engine, _, _, _, _, worker, provider, _ = prepared(
        database, {**RESULT, "category": category}
    )
    try:
        assert worker.once()
        assert not vip_rows(engine)
    finally:
        provider.close()


@pytest.mark.parametrize("action", ["delete", "bug", "humor", "source", "lease"])
def test_late_watched_result_cannot_grant_after_deletion_manual_override_or_source_change(
    database, action
):
    _, engine, _, review, _, service, worker, provider, _ = watched(database)

    def during_response(_):
        if action == "delete":
            service.delete(review, "admin")
        elif action == "bug":
            service.mark_bug(review, "admin")
        elif action == "humor":
            service.mark_humor(review, "admin")
        elif action == "source":
            with engine.begin() as conn:
                conn.execute(
                    text("UPDATE reviews SET transcript='Другой отзыв' WHERE id=:id"),
                    {"id": review},
                )
        else:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE jobs SET lease_until=now()-interval '1 second' WHERE review_id=:id"
                    ),
                    {"id": review},
                )

    try:
        provider.client.event_hooks["response"] = [during_response]
        assert worker.once()
        assert not vip_rows(engine)
    finally:
        provider.close()


@pytest.mark.parametrize("legacy", [False, True])
def test_watched_admin_confirmation_and_delete_revoke_vip_on_both_screens(database, legacy):
    settings, engine, city, review, event, _, worker, provider, _ = watched(database)
    try:
        assert worker.once()
        with engine.connect() as conn:
            number = conn.execute(
                text("SELECT number FROM reviews WHERE id=:id"), {"id": review}
            ).scalar_one()
        base = f"/guide/admin/reviews-2/{city['slug']}/{number}"
        with TestClient(create_app(settings)) as client:
            assert client.post(base + "/watched").status_code == 401
            login(client)
            page = client.get("/guide/admin/reviews-2/watched")
            assert page.status_code == 200 and "VIP · уровень 1" in page.text
            assert ">Досмотрел</button>" in page.text and ">Удалить</button>" in page.text
            assert ">ОК</button>" not in page.text
            assert (
                client.post(base + "/watched", headers={"origin": "http://testserver"}).status_code
                == 403
            )
            data = {"_csrf": token(page.text), "status": "watched"}
            for _ in range(2):
                response = client.post(
                    base + "/watched", data=data, headers={"origin": "http://testserver"}
                )
                assert response.status_code == 200 and "Вручную: Досмотрел" in response.text
            assert vip_rows(engine) == [(event["metadata"]["visitor"], 1)]
            assert state(engine, review)["manual_category"] == "watched"
            delete = (
                f"/guide/admin/reviews/{city['slug']}/{number}/delete"
                if legacy
                else base + "/delete"
            )
            for _ in range(2):
                assert (
                    client.post(
                        delete, data=data, headers={"origin": "http://testserver"}
                    ).status_code
                    == 200
                )
            assert not vip_rows(engine)
            assert (
                client.post(
                    base + "/watched", data=data, headers={"origin": "http://testserver"}
                ).status_code
                == 404
            )
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM audit WHERE target=:id AND action='review_marked_watched'"
                    ),
                    {"id": review},
                ).scalar_one()
                == 1
            )
    finally:
        provider.close()


def test_parallel_confirmation_keeps_single_level_one_and_delete_does_not_regrant_from_other_review(
    database,
):
    _, engine, _, review, event, service, worker, provider, _ = watched(database)
    try:
        assert worker.once()
        other = str(uuid4())
        Consumer(engine, None).import_event(
            {
                **event,
                "event_id": str(uuid4()),
                "sequence": 2,
                "reference": other,
                "transcript": WATCHED["edited_text"],
            }
        )
        assert worker.once()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: service.mark_watched(review, "admin"), range(2)))
        assert len(vip_rows(engine)) == 1 and vip_rows(engine)[0][1] == 1
        service.delete(review, "admin")
        assert not worker.once()
        assert not vip_rows(engine)
        assert state(engine, other)["result"]["category"] == "watched"
    finally:
        provider.close()


def test_vip_schema_is_minimal_and_migration_round_trip_preserves_reviews(database):
    settings, engine, _, review, _, _, worker, provider, _ = watched(database)
    try:
        assert worker.once()
        columns = inspect(engine).get_columns("vip_visitors", schema=settings.database_schema)
        assert [column["name"] for column in columns] == ["visitor_id", "level"]
        assert "vip_visitors" in owned_tables(settings)
        config = Config(str(settings.root_dir / "alembic.ini"))
        command.downgrade(config, "manual_review_humor")
        command.upgrade(config, "head")
        assert not vip_rows(engine)
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT transcript FROM reviews WHERE id=:id"), {"id": review}
                ).scalar_one()
                == WATCHED["edited_text"]
            )
    finally:
        provider.close()


def test_invalid_watched_response_does_not_grant_vip(database):
    _, engine, _, review, _, _, worker, provider, _ = prepared(
        database, {**RESULT, "category": "watched"}
    )
    try:
        assert worker.once()
        assert not vip_rows(engine)
        assert state(engine, review)["result"] is None
        with engine.connect() as conn:
            job = (
                conn.execute(
                    text("SELECT status,error FROM jobs WHERE review_id=:id"), {"id": review}
                )
                .mappings()
                .one()
            )
            assert job["status"] == "failed" and "Досмотрел" in job["error"]
    finally:
        provider.close()
