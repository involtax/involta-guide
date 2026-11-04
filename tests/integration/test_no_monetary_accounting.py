from uuid import uuid4

from sqlalchemy import text

from involta_voice.db import migrate, snapshot
from involta_voice.provider import FakeProvider
from involta_voice.service import VoiceService
from involta_voice.worker import Worker

from .test_voice_service import metadata, recording
from .test_voice_service import voice_database as voice_database


def test_voice_migration_keeps_history_but_new_calls_have_no_monetary_values(voice_database):
    settings, engine = voice_database
    historical_id = str(uuid4())
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM voice_schema_versions WHERE version=5"))
        conn.execute(
            text(
                "INSERT INTO provider_calls(id,submission,run,kind,request,cost_usd,reserved_usd) VALUES(:id,:submission,1,'analysis','{}',0.75,2)"
            ),
            {"id": historical_id, "submission": str(uuid4())},
        )
        conn.execute(text("UPDATE daily_usage SET cost=0.75"))
        conn.execute(
            text("INSERT INTO voice_settings(key,value) VALUES('prices',CAST(:prices AS jsonb))"),
            {"prices": '{"gpt-4.1":{"input":999}}'},
        )
    migrate(engine)
    migrate(engine)
    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT cost_usd FROM provider_calls WHERE id=:id"), {"id": historical_id}
            ).scalar_one()
            == 0.75
        )
        assert "prices" not in snapshot(conn, settings)
    service = VoiceService(engine, settings)
    ref = service.submit(metadata(), recording(), "audio/wav")["reference"]
    worker = Worker(engine, settings, FakeProvider())
    try:
        assert worker.once() and worker.once()
        assert service.status(ref)["state"] == "result_ready"
        with engine.connect() as conn:
            records = (
                conn.execute(
                    text("SELECT cost_usd,reserved_usd FROM provider_calls WHERE id<>:id"),
                    {"id": historical_id},
                )
                .mappings()
                .all()
            )
            assert len(records) == 2 and all(
                r["cost_usd"] is None and r["reserved_usd"] is None for r in records
            )
            usage = conn.execute(text("SELECT calls,cost FROM daily_usage")).mappings().one()
            assert usage["calls"] == 2 and usage["cost"] == 0.75
    finally:
        worker.provider.close()
