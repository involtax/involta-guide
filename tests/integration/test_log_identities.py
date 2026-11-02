import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from involta_guide.infrastructure.ai.audit import AIAudit
from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow, create_database
from involta_guide.infrastructure.seed import seed
from involta_guide.main import create_app

from .test_jobs import setup
from .test_web import login


def test_slug_rename_updates_stored_keys_and_paths_without_touching_responses(database):
    settings, engine, uow, queries, location, service, queue = setup(database)
    service.enqueue_public_fill(location["id"])
    job = queries.rows("jobs")[0]
    audit = AIAudit(engine, settings)
    audit.record(job["id"], "response", {"status": 200, "body": "original immutable answer"})
    with engine.begin() as conn:
        before = conn.execute(
            text("SELECT detail FROM audit WHERE action='ai_trace' AND target=:id"),
            {"id": job["id"]},
        ).scalar_one()
        conn.execute(
            text("UPDATE locations SET slug='ivanovo-new' WHERE id=:id"), {"id": location["id"]}
        )
        stored = conn.execute(
            text("SELECT location_slug,number,path FROM request_logs WHERE job_id=:id"),
            {"id": job["id"]},
        ).one()
        assert tuple(stored) == ("ivanovo-new", 1, "/logs/ivanovo-new/1")
        assert (
            conn.execute(
                text("SELECT detail FROM audit WHERE action='ai_trace' AND target=:id"),
                {"id": job["id"]},
            ).scalar_one()
            == before
        )
        primary = conn.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='request_logs'::regclass AND contype='p'"
            )
        ).scalar_one()
        assert primary == "PRIMARY KEY (location_slug, number)"
    with TestClient(create_app(settings)) as client:
        assert client.get("/guide/logs/ivanovo-new/1").status_code == 200
        old_path = client.get("/guide/logs/ivanovo/1", follow_redirects=False)
        assert (
            old_path.status_code == 308
            and old_path.headers["location"] == "/guide/logs/ivanovo-new/1"
        )
        assert (
            client.get("/guide/logs/" + job["id"], follow_redirects=False).headers["location"]
            == "/guide/logs/ivanovo-new/1"
        )
        login(client)
        admin = client.get("/guide/admin")
        assert 'href="/guide/logs"' in admin.text and "Аудит ИИ" not in admin.text
        old = client.get("/guide/admin?tab=ai-audit&job=" + job["id"], follow_redirects=False)
        assert old.status_code == 308 and old.headers["location"] == "/guide/logs/ivanovo-new/1"
        log = client.get("/guide/logs/ivanovo-new/1")
        assert 'name="job_id"' not in log.text
        assert 'name="log_slug" value="ivanovo-new"' in log.text


def test_concurrent_private_requests_receive_distinct_persisted_numbers(database):
    settings, engine, uow, queries, location, service, queue = setup(database)

    def enqueue(_):
        job_id = str(uuid4())
        with uow() as tx:
            tx.repo.queue({"id": job_id, "kind": "review", "location_id": location["id"]})
            return tx.repo.log_reference(job_id)["number"]

    with ThreadPoolExecutor(4) as pool:
        assert sorted(pool.map(enqueue, range(4))) == [1, 2, 3, 4]
    with TestClient(create_app(settings)) as client:
        assert client.get("/guide/logs/ivanovo/1").status_code == 404
        assert client.get("/guide/logs/ivanovo/1/audio").status_code == 401
        login(client)
        assert client.get("/guide/logs/ivanovo/1").status_code == 200
        assert client.get("/guide/logs/ivanovo/1.json").status_code == 200


def test_migration_preserves_old_numbers_jobs_and_raw_responses(database, monkeypatch):
    settings, engine = database
    schema = "test_log_upgrade_" + uuid4().hex
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE SCHEMA " + schema)
    custom = settings.model_copy(update={"database_schema": schema})
    target = create_database(custom)
    monkeypatch.setenv("DATABASE_SCHEMA", schema)
    config = Config(str(settings.root_dir / "alembic.ini"))
    try:
        command.upgrade(config, "0002_python")
        seed(PostgresUow(target), settings.root_dir)
        ids = [uuid4() for _ in range(3)]
        raw = json.dumps({"event": "response", "data": {"body": "original answer"}})
        with target.begin() as conn:
            location_id = conn.execute(
                text("SELECT id FROM locations WHERE slug='ivanovo'")
            ).scalar_one()
            for i, kind in enumerate(("generate", "generate", "review")):
                conn.execute(
                    text(
                        "INSERT INTO jobs(id,kind,location_id,status,created_at) VALUES(:id,:kind,:location,'failed',CAST(:stamp AS timestamptz))"
                    ),
                    {
                        "id": ids[i],
                        "kind": kind,
                        "location": location_id,
                        "stamp": f"2026-01-0{i + 1}T00:00:00Z",
                    },
                )
            conn.execute(
                text(
                    "INSERT INTO audit(id,action,target,detail) VALUES(:id,'generation_run',:job,:detail)"
                ),
                {"id": uuid4(), "job": str(ids[0]), "detail": '{"number":1,"model":"gpt-4.1"}'},
            )
            conn.execute(
                text(
                    "INSERT INTO audit(id,action,target,detail) VALUES(:id,'ai_trace',:job,:detail)"
                ),
                {"id": uuid4(), "job": str(ids[1]), "detail": raw},
            )
        command.upgrade(config, "head")
        with target.connect() as conn:
            rows = conn.execute(
                text("SELECT job_id,number,path FROM request_logs ORDER BY number")
            ).all()
            assert [(r[0], r[1], r[2]) for r in rows] == [
                (ids[i], i + 1, f"/logs/ivanovo/{i + 1}") for i in range(3)
            ]
            assert conn.execute(text("SELECT count(*) FROM jobs")).scalar_one() == 3
            assert (
                conn.execute(text("SELECT detail FROM audit WHERE action='ai_trace'")).scalar_one()
                == raw
            )
        command.upgrade(config, "head")
    finally:
        target.dispose()
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP SCHEMA " + schema + " CASCADE")
