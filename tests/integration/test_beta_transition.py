"""Only the currently released Guide/Voice schemas and a clean install are supported."""

from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from involta_guide.infrastructure.beta_reset import cleanup_files
from involta_guide.infrastructure.persistence.unit_of_work import create_database
from involta_voice.db import migrate
from involta_voice.reset import cleanup_files as voice_cleanup
from involta_voice.service import VoiceService

from .test_voice_service import voice_database as voice_database


@pytest.mark.parametrize("previous_revision", ["admin_routes", "review_urls"])
def test_populated_released_transition_preserves_owners_and_clears_incompatible_beta(
    database, monkeypatch, previous_revision
):
    settings, admin_engine = database
    schema = "beta_transition_" + uuid4().hex
    with admin_engine.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA {schema}"))
    settings = settings.model_copy(update={"database_schema": schema})
    engine = create_database(settings)
    monkeypatch.setenv("DATABASE_SCHEMA", schema)
    config = Config(str(settings.root_dir / "alembic.ini"))
    city, hub, other, restaurant, hub_rest, review, job, session = [str(uuid4()) for _ in range(8)]
    try:
        command.upgrade(config, previous_revision)
        with engine.begin() as conn:
            for identity, slug, is_hub in [
                (city, "owner", False),
                (hub, "hub", True),
                (other, "other", False),
            ]:
                conn.execute(
                    text(
                        "INSERT INTO locations(id,name,region,slug,intro,is_hub) VALUES(:id,:slug,'Region',:slug,'Current intro',:hub)"
                    ),
                    {"id": identity, "slug": slug, "hub": is_hub},
                )
            for identity, owner in [(restaurant, city), (hub_rest, hub)]:
                conn.execute(
                    text(
                        "INSERT INTO restaurants(id,location_id,name,cuisine,description,maps_url,phone,alex_star) VALUES(:id,:loc,'Owned card','Cuisine','Description','https://yandex.ru/maps/org/test/123/','+74951234567',true)"
                    ),
                    {"id": identity, "loc": owner},
                )
            before = dict(
                conn.execute(text("SELECT * FROM restaurants WHERE id=:id"), {"id": restaurant})
                .mappings()
                .one()
            )
            conn.execute(
                text("INSERT INTO restaurant_locations VALUES(:restaurant,:other)"),
                {"restaurant": restaurant, "other": other},
            )
            conn.execute(
                text(
                    "INSERT INTO reviews(id,location_id,visitor,audio,mime,hash,ip_hash,device) VALUES(:id,:loc,'old-hashed-visitor','legacy.wav','audio/wav','hash','old-ip-hash','UA')"
                ),
                {"id": review, "loc": city},
            )
            old_review_number = (
                conn.execute(text("SELECT number FROM reviews")).scalar_one()
                if previous_revision == "review_urls"
                else 1
            )
            conn.execute(
                text(
                    "INSERT INTO voice_links(review_id,reference,location_id,number,active_run,metadata) VALUES(:id,:id,:loc,7,1,'{}')"
                ),
                {"id": review, "loc": city},
            )
            conn.execute(
                text(
                    "INSERT INTO jobs(id,kind,location_id,review_id) VALUES(:id,'review',:loc,:review)"
                ),
                {"id": job, "loc": city, "review": review},
            )
            conn.execute(
                text(
                    "INSERT INTO admin_sessions(id,token_hash,csrf_hash,expires_at) VALUES(:id,'keep-admin','keep-csrf',now()+interval '1 day')"
                ),
                {"id": session},
            )
            conn.execute(text("INSERT INTO settings(key,value) VALUES('beta-marker','keep')"))
            conn.execute(
                text(
                    "INSERT INTO audit(id,action,target,detail) VALUES(:id,'manual_intro',:loc,'old personal content')"
                ),
                {"id": str(uuid4()), "loc": city},
            )
        path = settings.data_dir / "audio" / "legacy.wav"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"old Guide audio")
        command.upgrade(config, "head")
        command.upgrade(config, "head")  # No repeated deletion on startup/migration.
        with engine.connect() as conn:
            after = dict(conn.execute(text("SELECT * FROM restaurants")).mappings().one())
            assert {k: after[k] for k in before} == before
            location = (
                conn.execute(text("SELECT * FROM locations WHERE id=:id"), {"id": city})
                .mappings()
                .one()
            )
            assert location["intro"] == "Current intro" and location["initialized_at"]
            assert location["next_log_number"] == 1 and location["next_review_number"] == 7
            assert (
                conn.execute(text("SELECT intro FROM locations WHERE id=:id"), {"id": hub}).scalar()
                == ""
            )
            assert (
                conn.execute(
                    text("SELECT restaurants_removed FROM beta_transition_report")
                ).scalar()
                == 1
            )
            assert conn.execute(text("SELECT count(*) FROM admin_sessions")).scalar() == 1
            assert (
                conn.execute(text("SELECT value FROM settings WHERE key='beta-marker'")).scalar()
                == "keep"
            )
            assert conn.execute(text("SELECT action,target FROM audit ORDER BY target")).all() == [
                ("prompt_contract", "filling_top3"),
                ("prompt_contract", "filling_v3"),
            ]
            for table in ["reviews", "jobs", "request_logs"]:
                assert conn.execute(text("SELECT count(*) FROM " + table)).scalar() == 0
        cleanup_files(engine, settings)
        assert not path.exists()
        with engine.begin() as conn:
            new_number = conn.execute(
                text(
                    "INSERT INTO reviews(id,location_id,visitor,audio,mime,hash,ip,user_agent) VALUES(:id,:loc,'new-raw','voice:new','audio/wav','hash','203.0.113.1','UA') RETURNING number"
                ),
                {"id": str(uuid4()), "loc": city},
            ).scalar_one()
            assert new_number > old_review_number
    finally:
        engine.dispose()
        with admin_engine.begin() as conn:
            conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))


def test_populated_voice_v3_transition_deletes_files_explicitly_and_keeps_sequence(voice_database):
    settings, engine = voice_database
    # This DB/role is disposable and belongs solely to this test.
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP SCHEMA public CASCADE")
        conn.exec_driver_sql("CREATE SCHEMA public")
        from pathlib import Path

        import involta_voice

        root = Path(involta_voice.__file__).parent / "migrations"
        for filename in ["001_initial.sql", "002_guide_transcription.sql", "003_asr_beta.sql"]:
            conn.exec_driver_sql((root / filename).read_text())
        ref, loc = str(uuid4()), str(uuid4())
        conn.execute(
            text(
                "INSERT INTO submissions(reference,location_id,visitor,idempotency_key,payload_hash,metadata,audio,mime,duration,size,sha256,repeat_signals) VALUES(:ref,:loc,'old-fingerprint',:ref,'hash','{}','old.wav','audio/wav',2,10,'sha','{}')"
            ),
            {"ref": ref, "loc": loc},
        )
        conn.execute(
            text(
                "INSERT INTO outbox(event_id,submission,run,kind,payload) VALUES(:ref,:ref,1,'transcript_ready','{}')"
            ),
            {"ref": ref},
        )
        sequence = conn.execute(text("SELECT max(sequence) FROM outbox")).scalar_one()
        conn.execute(text("INSERT INTO delivery_ack VALUES('guide',:seq)"), {"seq": sequence})
    service = VoiceService(engine, settings)
    service.storage.root.mkdir(parents=True, exist_ok=True)
    path = service.storage.root / "old.wav"
    path.write_bytes(b"old Voice audio")
    migrate(engine)
    assert path.exists()  # The manifest survives the schema transaction; deletion is explicit.
    voice_cleanup(service)
    assert not path.exists()
    migrate(engine)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM submissions")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM purge_files")).scalar() == 0
        assert conn.execute(text("SELECT sequence FROM delivery_ack")).scalar() == sequence
        assert conn.execute(text("SELECT nextval('outbox_sequence_seq')")).scalar() > sequence
