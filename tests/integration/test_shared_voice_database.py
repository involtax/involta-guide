import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import text
from sqlalchemy.engine import make_url

from involta_guide.infrastructure.voice.shared_database import consolidate, digest
from involta_voice.db import database, migrate_connection
from involta_voice.provider import FakeProvider
from involta_voice.service import VoiceService
from involta_voice.worker import Worker

from .test_voice_service import metadata, recording
from .test_voice_service import voice_database as voice_database


@pytest.fixture
def legacy_source(voice_database):
    settings, _ = voice_database
    settings = settings.model_copy(update={"database_schema": "public"})
    engine = database(settings)
    with engine.begin() as conn:
        migrate_connection(conn, version=5)
    try:
        yield settings, engine
    finally:
        engine.dispose()


@pytest.fixture
def shared_target(voice_database):
    settings, _ = voice_database
    schema = "voice_shared_" + uuid4().hex
    target = settings.model_copy(
        update={
            "database_url": type(settings.database_url)(os.environ["TEST_DATABASE_URL"]),
            "database_schema": schema,
        }
    )
    yield target
    with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as conn:
        conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def source_url(settings):
    return (
        make_url(settings.database_url.get_secret_value())
        .update_query_dict(
            {
                "options": "-c search_path=" + settings.database_schema,
            }
        )
        .render_as_string(hide_password=False)
        .replace("+", "%20")
    )


def test_transfer_preserves_audio_events_sequences_and_old_client_access(
    legacy_source, shared_target
):
    settings, engine = legacy_source
    service = VoiceService(engine, settings)
    reference = service.submit(metadata(), recording(), "audio/wav")["reference"]
    worker = Worker(engine, settings, FakeProvider())
    assert worker.once() and not worker.once()
    assert consolidate(shared_target, source_url(settings)) == "imported"
    assert consolidate(shared_target, source_url(settings)) == "verified"
    target = database(shared_target)
    try:
        moved = VoiceService(target, shared_target)
        assert moved.status(reference)["transcript"] == service.status(reference)["transcript"]
        with target.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM outbox")).scalar_one() == 2
            sequence = conn.execute(text("SELECT nextval('outbox_sequence_seq')")).scalar_one()
            assert sequence == 3
        # An old image reads VOICE_DATABASE_URL and has no schema configuration.
        with psycopg.connect(source_url(shared_target)) as old_client:
            assert old_client.execute("SELECT count(*) FROM submissions").fetchone()[0] == 1
        assert consolidate(shared_target, source_url(shared_target)) == "shared"
        assert list((shared_target.data_dir / "audio").glob("*"))
    finally:
        target.dispose()


def test_failed_import_rolls_back_schema_and_never_changes_source(
    legacy_source, shared_target, monkeypatch
):
    settings, engine = legacy_source
    VoiceService(engine, settings).submit(metadata(), recording(), "audio/wav")
    with psycopg.connect(source_url(settings)) as source:
        before = digest(source, settings.database_schema, "submissions")
    monkeypatch.setattr(
        "involta_guide.infrastructure.voice.shared_database.digest", lambda *args: uuid4().bytes
    )
    with pytest.raises(RuntimeError, match="verification failed"):
        consolidate(shared_target, source_url(settings))
    with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM pg_namespace WHERE nspname=%s", (shared_target.database_schema,)
            ).fetchone()
            is None
        )
    with psycopg.connect(source_url(settings)) as source:
        assert digest(source, settings.database_schema, "submissions") == before
