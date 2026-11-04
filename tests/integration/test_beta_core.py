"""Behavioural coverage for the direct beta architecture transition."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import inspect, text

from involta_guide.application.services.jobs import JobService
from involta_guide.application.services.locations import Queries
from involta_guide.domain.entities import Conflict
from involta_guide.infrastructure.beta_reset import BetaReset
from involta_guide.infrastructure.jobs.queue import JobQueue
from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow
from involta_guide.infrastructure.voice.consumer import Consumer
from involta_voice.contracts import LocationContext
from involta_voice.errors import VoiceError
from involta_voice.reset import purge
from involta_voice.service import VoiceService
from involta_voice.worker import Worker

from .test_jobs import FakeAI
from .test_voice_service import metadata, recording
from .test_voice_service import voice_database as voice_database


def test_fresh_schema_has_single_owner_and_explicit_lifecycle(database):
    settings, engine = database
    tables = inspect(engine).get_table_names(schema=settings.database_schema)
    assert "restaurant_locations" not in tables
    assert "alex_sessions" not in tables
    assert "voice_archives" not in tables
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM locations")).scalar() > 1000
        assert (
            conn.execute(
                text("SELECT count(*) FROM locations WHERE content_epoch<>1 OR reset_pending")
            ).scalar()
            == 0
        )
        assert "page_data" not in [c["name"] for c in inspect(conn).get_columns("locations")]


def core(database):
    settings, engine = database

    def factory():
        return PostgresUow(engine)

    queries = Queries(factory)
    city = queries.directory(query="Иваново")["rows"][0]
    jobs = JobService(factory, FakeAI())
    return settings, engine, queries, city, jobs, JobQueue(engine, settings)


def submission(city, epoch=1):
    return metadata().model_copy(
        update={
            "location": LocationContext(
                id=city["id"], slug=city["slug"], name=city["name"], content_epoch=epoch
            ),
            "visitor": str(uuid4()),
            "ip": "203.0.113.17",
            "user_agent": "Beta test UA",
        }
    )


def test_first_fill_success_blocks_public_retry_and_admin_regeneration(database):
    _, engine, queries, city, jobs, queue = core(database)
    with ThreadPoolExecutor(3) as pool:
        list(pool.map(lambda _: jobs.enqueue_public_fill(city["id"]), range(3)))
    assert len(queries.rows("jobs")) == 1
    job = queue.claim()
    jobs.process(job)
    assert queries.get("locations", city["id"])["initialized_at"]
    assert jobs.public_fill_state(city["id"])["state"] == "ready"
    assert jobs.enqueue_public_fill(city["id"])["can_start"] is False
    with pytest.raises(Conflict):
        jobs.regenerate(job["id"])
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM audit"))
    assert jobs.public_fill_state(city["id"])["can_start"] is False


@pytest.mark.parametrize(
    "failure_phase", ["voice_files", "voice_confirm", "guide_files", "guide_commit"]
)
def test_reset_survives_external_failure_without_resurrection(
    database, voice_database, monkeypatch, failure_phase
):
    settings, engine, queries, city, jobs, queue = core(database)
    voice_settings, voice_engine = voice_database
    voice = VoiceService(voice_engine, voice_settings)
    oldmeta = submission(city)
    ref = voice.submit(oldmeta, recording(), "audio/wav")["reference"]
    worker = Worker(voice_engine, voice_settings)
    while worker.once():
        pass
    with voice_engine.connect() as conn:
        events = [
            {"sequence": r.sequence, **r.payload}
            for r in conn.execute(text("SELECT sequence,payload FROM outbox ORDER BY sequence"))
        ]
        path = voice.storage.path(conn.execute(text("SELECT audio FROM submissions")).scalar_one())
        calls_before_reset = conn.execute(
            text("SELECT coalesce(sum(calls),0) FROM daily_usage")
        ).scalar_one()
    consumer = Consumer(engine, None)
    for event in events:
        consumer.import_event(event)
    jobs.enqueue_public_fill(city["id"])
    generation = queue.claim()
    jobs.process(generation)
    previous_log = queries.rows("request_logs")[0]["number"]
    previous_review = queries.rows("reviews")[0]["number"]
    other = queries.directory(query="Кострома")["rows"][0]
    othermeta = submission(other)
    otherref = voice.submit(othermeta, recording(), "audio/wav")["reference"]
    legacy_path = settings.data_dir / "audio" / "old.wav"
    legacy_path.parent.mkdir(exist_ok=True)
    legacy_path.write_bytes(b"private old bytes")
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO beta_cleanup_files(path,location_id) VALUES(:path,:id)"),
            {"path": legacy_path.name, "id": city["id"]},
        )

    class Client:
        def json(self, method, path, json):
            return purge(voice, city["id"], json["content_epoch"])

    reset = BetaReset(engine, settings, jobs, Client())
    request_key = str(uuid4())
    with ThreadPoolExecutor(2) as pool:
        keys = list(
            pool.map(
                lambda key: reset.begin(city["id"], key, 1, city["name"]),
                [request_key, str(uuid4())],
            )
        )
    assert len(set(keys)) == 1
    request_key = keys[0]
    claimed = queue.claim()
    assert claimed["kind"] == "reset"
    import involta_guide.infrastructure.beta_reset as guide_reset
    import involta_voice.reset as voice_reset

    with monkeypatch.context() as patch:
        if failure_phase == "voice_files":
            patch.setattr(
                type(path),
                "unlink",
                lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk unavailable")),
            )
        elif failure_phase == "voice_confirm":
            original = voice_reset.cleanup_files

            def fail_confirm(*args):
                original(*args)
                raise RuntimeError("crash after file deletion")

            patch.setattr(voice_reset, "cleanup_files", fail_confirm)
        elif failure_phase == "guide_files":
            patch.setattr(
                guide_reset,
                "cleanup_files",
                lambda *args: (_ for _ in ()).throw(OSError("guide disk unavailable")),
            )
        else:
            patch.setattr(
                jobs,
                "_queue_generation",
                lambda *args: (_ for _ in ()).throw(RuntimeError("crash before guide commit")),
            )
        with pytest.raises((OSError, RuntimeError)):
            reset.perform(claimed)
    current = queries.get("locations", city["id"])
    assert current["reset_pending"] and current["content_epoch"] == 2
    assert jobs.public_fill_state(city["id"])["can_start"] is False
    assert consumer.import_event(events[-1]) == 0
    with pytest.raises(VoiceError, match="сброшена"):
        voice.submit(oldmeta, recording(), "audio/wav")
    reset.perform(claimed)
    assert reset.begin(city["id"], request_key, 1, city["name"]) == request_key
    assert not queries.get("locations", city["id"])["reset_pending"]
    assert queries.rows("reviews") == []
    assert queries.rows("restaurants") == []
    assert not path.exists() and not legacy_path.exists()
    assert voice.status(otherref)["reference"] == otherref
    with voice_engine.connect() as conn:
        assert (
            conn.execute(text("SELECT coalesce(sum(calls),0) FROM daily_usage")).scalar_one()
            == calls_before_reset
        )
        assert (
            conn.execute(
                text("SELECT count(*) FROM submissions WHERE reference=:id"), {"id": ref}
            ).scalar()
            == 0
        )
    assert queries.rows("request_logs")[0]["number"] > previous_log
    assert len([j for j in queries.rows("jobs") if j["kind"] == "generate"]) == 1
    with pytest.raises(Conflict):
        reset.begin(city["id"], str(uuid4()), 1, city["name"])
    fresh = voice.submit(submission(city, 2), recording(), "audio/wav")["reference"]
    assert (
        purge(voice, city["id"], 2)["purged"] is True
    )  # Duplicate purge cannot delete new-cycle content.
    assert voice.status(fresh)["reference"] == fresh
    while worker.once():
        pass
    with voice_engine.connect() as conn:
        fresh_events = [
            {"sequence": r.sequence, **r.payload}
            for r in conn.execute(
                text("SELECT sequence,payload FROM outbox WHERE submission=:id ORDER BY sequence"),
                {"id": fresh},
            )
        ]
    for event in fresh_events:
        consumer.import_event(event)
    assert queries.get("reviews", fresh)["number"] > previous_review
    assert queries.get("reviews", fresh)["ip"] == oldmeta.ip
    assert queries.get("reviews", fresh)["user_agent"] == oldmeta.user_agent


def test_poison_event_is_durable_and_does_not_block_other_reviews(database, voice_database):
    _, engine, queries, city, _, _ = core(database)
    vs, ve = voice_database
    voice = VoiceService(ve, vs)
    refs = [voice.submit(submission(city), recording(), "audio/wav")["reference"] for _ in range(2)]
    worker = Worker(ve, vs)
    while worker.once():
        pass
    with ve.connect() as conn:
        original = [
            {"sequence": r.sequence, **r.payload}
            for r in conn.execute(text("SELECT sequence,payload FROM outbox ORDER BY sequence"))
        ]
    import copy

    events = copy.deepcopy(original)
    bad = next(e for e in events if e["reference"] == refs[0] and e["kind"] == "gigaam_ready")
    bad["transcript"] = ""

    class Client:
        acknowledged = 0

        def json(self, method, path, **kwargs):
            if method == "GET":
                return {"events": [e for e in events if e["sequence"] > kwargs["params"]["after"]]}
            self.acknowledged = kwargs["json"]["sequence"]
            return {"ok": True}

    client = Client()
    consumer = Consumer(engine, client)
    consumer.once()
    assert client.acknowledged == max(e["sequence"] for e in events)
    assert {r["id"] for r in queries.rows("reviews")} == set(refs)
    assert queries.get("reviews", refs[0])["transcript"] is None
    assert queries.get("reviews", refs[1])["transcript"]
    with engine.connect() as conn:
        failed = (
            conn.execute(text("SELECT payload,error FROM voice_inbox WHERE state='failed'"))
            .mappings()
            .one()
        )
        assert failed["payload"] == bad and failed["error"]
        assert (
            conn.execute(text("SELECT count(*) FROM voice_inbox WHERE state='pending'")).scalar()
            == 0
        )
    # Correcting a saved poison input is an operator action; retry reuses the inbox identity.
    repaired = next(e for e in original if e["event_id"] == bad["event_id"])
    import json

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE voice_inbox SET payload=CAST(:payload AS jsonb) WHERE event_id=:id"),
            {"payload": json.dumps(repaired), "id": bad["event_id"]},
        )
    assert consumer.retry(bad["event_id"]) == 1
    consumer.once()
    assert len(queries.rows("reviews")) == 2
    assert all(i["state"] == "imported" for i in queries.rows("voice_inbox"))


def test_receipt_failure_never_acknowledges_later_events(database, monkeypatch):
    _, engine, _, _, _, _ = core(database)

    class Client:
        acknowledged = False

        def json(self, method, path, **kwargs):
            if method == "GET":
                return {"events": [{"sequence": 1}, {"sequence": 2}]}
            self.acknowledged = True

    client = Client()
    consumer = Consumer(engine, client)

    def fail(event):
        raise RuntimeError("database unavailable before durable receipt")

    monkeypatch.setattr(consumer, "_receive", fail)
    with pytest.raises(RuntimeError):
        consumer.once()
    assert not client.acknowledged


def test_late_voice_worker_cannot_recreate_data_or_trace_after_reset(database, voice_database):
    _, _, _, city, _, _ = core(database)
    vs, ve = voice_database
    voice = VoiceService(ve, vs)
    meta = submission(city)
    voice.submit(meta, recording(), "audio/wav")
    from threading import Event

    from involta_voice.provider import FakeProvider

    started, released = Event(), Event()

    class Delayed(FakeProvider):
        def call(self, *args):
            started.set()
            assert released.wait(10)
            return super().call(*args)

    worker = Worker(ve, vs, Delayed())
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(worker.once)
        assert started.wait(10)
        purge(voice, city["id"], 2)
        released.set()
        future.result(timeout=10)
    with ve.connect() as conn:
        for table in (
            "submissions",
            "runs",
            "stages",
            "provider_calls",
            "outbox",
            "asr_candidates",
            "admin_events",
            "purge_files",
            "intake_windows",
        ):
            assert conn.execute(text("SELECT count(*) FROM " + table)).scalar() == 0, table
    assert list(voice.storage.root.iterdir()) == []


def test_owner_boundaries_noop_and_independent_city_locks(database):
    from involta_guide.application.services.locations import LocationService

    from .editor_fixture import FixtureEditor
    from .test_editorial import CONTENT

    _, engine, queries, city, _, _ = core(database)

    def factory():
        return PostgresUow(engine)

    editor = FixtureEditor(factory)
    revision = editor.save(city["id"], CONTENT, None)
    before = queries.location(city["id"])["content"]
    assert editor.save(city["id"], before, revision) == revision
    assert len([r for r in queries.rows("reviews") if r["changes"]]) == 1
    other = queries.directory(query="Кострома")["rows"][0]
    with factory() as locked:
        locked.repo.get("locations", city["id"], lock=True)
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(
                LocationService(factory).save_intro,
                other["id"],
                "Независимая правка",
                None,
                str(uuid4()),
            )
            assert future.result(timeout=3)
    from sqlalchemy.exc import IntegrityError, InternalError, ProgrammingError

    card = queries.rows("restaurants")[0]
    with pytest.raises((IntegrityError, InternalError, ProgrammingError)):
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE restaurants SET location_id=:other WHERE id=:id"),
                {"other": other["id"], "id": card["id"]},
            )
    with engine.begin() as conn:
        conn.execute(text("UPDATE locations SET is_hub=true WHERE id=:id"), {"id": other["id"]})
    with pytest.raises((IntegrityError, InternalError, ProgrammingError)):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO restaurants(id,location_id,name,cuisine,description,maps_url) VALUES(:id,:loc,'X','X','X','https://yandex.ru/maps/org/x/1/')"
                ),
                {"id": str(uuid4()), "loc": other["id"]},
            )


def test_late_guide_result_and_debug_are_fenced_after_reset(database, voice_database):
    from threading import Event

    from involta_guide.infrastructure.ai.audit import AIAudit
    from involta_guide.infrastructure.jobs.worker import Worker as GuideWorker
    from involta_guide.infrastructure.voice.upload_debug import UploadDebug

    settings, engine, queries, city, jobs, queue = core(database)
    vs, ve = voice_database
    voice = VoiceService(ve, vs)
    started, released = Event(), Event()

    class Delayed(FakeAI):
        def generate(self, *args):
            started.set()
            assert released.wait(10)
            return super().generate(*args)

    jobs.provider = Delayed()
    jobs.enqueue_public_fill(city["id"])
    oldjob = queries.rows("jobs")[0]

    class Client:
        def json(self, method, path, json):
            return purge(voice, city["id"], json["content_epoch"])

    reset = BetaReset(engine, settings, jobs, Client())
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(GuideWorker(queue, jobs).once)
        assert started.wait(10)
        reset.begin(city["id"], str(uuid4()), 1, city["name"])
        claimed = queue.claim()
        reset.perform(claimed)
        released.set()
        assert future.result(timeout=10)
    AIAudit(engine, settings).record(oldjob["id"], "job_failed", {"old": "content"})
    UploadDebug(engine, settings).append(
        str(uuid4()),
        {"_context": {"slug": city["slug"], "content_epoch": 1}, "report": {"old": "content"}},
    )
    assert queries.rows("restaurants") == []
    assert not queries.get("locations", city["id"])["initialized_at"]
    assert len(queries.rows("jobs")) == 1
    assert all(a["target"] != oldjob["id"] for a in queries.rows("audit"))
    assert queries.rows("audit", {"action": "voice_upload_http"}) == []


def test_expired_reset_worker_cannot_write_error_over_current_lease(database):
    settings, engine, queries, city, jobs, queue = core(database)
    reset = BetaReset(engine, settings, jobs)
    reset.begin(city["id"], str(uuid4()), 1, city["name"])
    old = queue.claim()
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE jobs SET lease_until=now()-interval '1 second' WHERE id=:id"),
            {"id": old["id"]},
        )
    current = queue.claim()
    assert current["lease_owner"] != old["lease_owner"]
    queue.fail(old, "late failed worker")
    assert queries.get("locations", city["id"])["reset_error"] is None
    assert queries.get("jobs", current["id"])["status"] == "running"
