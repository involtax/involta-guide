"""Regression fixture represents a released schema, not an executable migration chain."""

import json
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from involta_guide.application.services.locations import Queries
from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow, create_database


@pytest.mark.parametrize("previous", ["0007_voice_package", "0006_coverage"])
def test_cutover_keeps_current_data_and_discards_page_history(database, monkeypatch, previous):
    settings, admin_engine = database
    schema = "cutover_" + uuid4().hex
    with admin_engine.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA {schema}"))
    upgraded = settings.model_copy(update={"database_schema": schema})
    engine = create_database(upgraded)
    city, restaurant, old, current = [str(uuid4()) for _ in range(4)]
    review_id, voice_job = str(uuid4()), str(uuid4())
    report = [{"name": "Огонёк", "totalScore": 8.5}]
    payload = {
        "intro": "Текст старого снимка",
        "restaurants": [{"id": restaurant, "description": "Старая карточка"}],
        "rankingReport": report,
        "candidateReview": [{"name": "Огонёк", "status": "verified"}],
        "qualityReport": {"version": 2, "complete": True},
    }
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                (settings.root_dir / "tests/fixtures/schema/pre_current_state.sql").read_text()
            )
            conn.execute(
                text("INSERT INTO alembic_version VALUES(:revision)"), {"revision": previous}
            )
            conn.execute(
                text(
                    "INSERT INTO locations(id,name,region,slug,intro,top_limit) VALUES(:id,'Иваново','Область','ivanovo','Актуальное вступление',2)"
                ),
                {"id": city},
            )
            conn.execute(
                text(
                    "INSERT INTO restaurants(id,location_id,name,cuisine,description,maps_url,phone,alex_star) VALUES(:id,:city,'Огонёк','Русская','Актуальное описание','https://yandex.ru/maps/org/test/123/','+74951234567',true)"
                ),
                {"id": restaurant, "city": city},
            )
            for number, identity in enumerate([old, current], 1):
                conn.execute(
                    text(
                        "INSERT INTO versions(id,location_id,number,content,source) VALUES(:id,:city,:n,CAST(:content AS jsonb),'ai')"
                    ),
                    {"id": identity, "city": city, "n": number, "content": json.dumps(payload)},
                )
            conn.execute(
                text("UPDATE locations SET published_version=:v,draft_version=:v WHERE id=:id"),
                {"v": current, "id": city},
            )
            conn.execute(
                text("INSERT INTO restaurant_locations VALUES(:r,:c)"), {"r": restaurant, "c": city}
            )
            conn.execute(text("INSERT INTO settings VALUES('generation_model','gpt-5')"))
            conn.execute(
                text(
                    "INSERT INTO jobs(id,kind,location_id,status) VALUES(:id,'generate',:city,'done')"
                ),
                {"id": str(uuid4()), "city": city},
            )
            conn.execute(
                text(
                    "INSERT INTO reviews(id,location_id,visitor,audio,mime,hash,ip_hash,device,transcript,status) VALUES(:id,:city,'test','test.webm','audio/webm','hash','ip','test','Был в Огоньке, было вкусно','processed')"
                ),
                {"id": review_id, "city": city},
            )
            conn.execute(
                text(
                    "INSERT INTO jobs(id,kind,location_id,review_id) VALUES(:id,'review',:city,:review)"
                ),
                {"id": voice_job, "city": city, "review": review_id},
            )
            if previous == "0006_coverage":
                # The immediately preceding non-Voice production release.
                conn.exec_driver_sql(
                    "DROP TABLE review_fragment_bindings,review_decisions,voice_archives,voice_inbox,voice_cursor,voice_consumer_health,voice_fragments,voice_proposals,voice_results,voice_links,review_texts,voice_candidate_proposals,voice_page_drafts"
                )
                conn.exec_driver_sql(
                    "ALTER TABLE reviews DROP COLUMN admin_session_id; ALTER TABLE alex_changes DROP COLUMN admin_session_id"
                )
        monkeypatch.setenv("DATABASE_SCHEMA", schema)
        command.upgrade(Config(str(settings.root_dir / "alembic.ini")), "head")
        command.upgrade(Config(str(settings.root_dir / "alembic.ini")), "head")
        queries = Queries(lambda: PostgresUow(engine))
        result = queries.public("ivanovo")["content"]
        assert result["intro"] == "Актуальное вступление"
        assert result["restaurants"][0]["description"] == "Актуальное описание"
        assert result["restaurants"][0]["id"] == restaurant
        assert result["restaurants"][0]["alexStar"] is True
        assert result["restaurants"][0]["phone"] == "+74951234567"
        assert result["rankingReport"] == report
        assert result["candidateReview"] == payload["candidateReview"]
        assert queries.get("locations", city)["top_limit"] == 2
        assert queries.setting("generation_model") == "gpt-5"
        assert queries.rows("jobs", {"kind": "generate"}) == []
        assert queries.get("jobs", voice_job)["status"] == "queued"
        assert (
            queries.get("review_texts", review_id)["original_text"] == "Был в Огоньке, было вкусно"
        )
        assert not {"versions", "version_reviews", "voice_page_drafts"} & set(
            inspect(engine).get_table_names()
        )
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "admin_routes"
            )
            assert (
                conn.execute(
                    text("SELECT sequence FROM voice_cursor WHERE consumer='voice'")
                ).scalar_one()
                == 0
            )
    finally:
        engine.dispose()
        with admin_engine.begin() as conn:
            conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
