import hashlib
import json
import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import text
from sqlalchemy.engine import make_url

from involta_guide.infrastructure.voice.shared_database import consolidate, digest
from involta_voice.db import database, migrate_connection
from involta_voice.service import VoiceService

from .test_voice_service import metadata, recording
from .test_voice_service import voice_database as voice_database


@pytest.fixture(params=[5, 6, 7, 8, 9])
def legacy_source(voice_database, request):
    settings, _ = voice_database
    settings = settings.model_copy(update={"database_schema": "public"})
    engine = database(settings)
    with engine.begin() as conn:
        migrate_connection(conn, version=request.param)
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


def seed_legacy_recording(settings, engine):
    """Populate each real historical schema without running new code against old columns."""
    data = metadata()
    legacy_metadata = data.model_dump(mode="json")
    legacy_metadata["location"]["id"] = str(uuid4())
    reference = str(uuid4())
    audio = recording()
    filename = reference + ".wav"
    service = VoiceService(engine, settings)
    service.storage.root.mkdir(parents=True, exist_ok=True)
    service.storage.path(filename).write_bytes(audio)
    transcript = "Saved original GigaAM text"
    snapshot = {"location": legacy_metadata["location"], "transcription_model": "gigaam"}
    with engine.begin() as conn:
        conn.execute(
            text("""INSERT INTO submissions(reference,location_id,visitor,idempotency_key,payload_hash,metadata,
          audio,mime,duration,size,sha256,repeat_signals,transcript)
          VALUES(:id,:location,:visitor,:key,'legacy-hash',CAST(:metadata AS jsonb),:audio,'audio/wav',6,:size,:sha,'{}',:transcript)"""),
            {
                "id": reference,
                "location": legacy_metadata["location"]["id"],
                "visitor": data.visitor,
                "key": str(data.idempotency_key),
                "metadata": json.dumps(legacy_metadata),
                "audio": filename,
                "size": len(audio),
                "sha": hashlib.sha256(audio).hexdigest(),
                "transcript": transcript,
            },
        )
        conn.execute(
            text(
                "INSERT INTO runs(submission,number,snapshot) VALUES(:id,1,CAST(:snapshot AS jsonb))"
            ),
            {"id": reference, "snapshot": json.dumps(snapshot)},
        )
        conn.execute(
            text(
                "INSERT INTO stages(submission,run,kind,state) VALUES(:id,1,'transcription','completed')"
            ),
            {"id": reference},
        )
        conn.execute(
            text("""INSERT INTO asr_candidates(submission,run,engine,revision,settings,transcript,elapsed_seconds,duration,details)
          VALUES(:id,1,'gigaam','legacy-revision','{}',:transcript,1,6,'{}')"""),
            {"id": reference, "transcript": transcript},
        )
        service.emit(conn, reference, 1, "recording_received")
        service.emit(conn, reference, 1, "gigaam_ready")
    return reference, transcript


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
    reference, original_text = seed_legacy_recording(settings, engine)
    assert consolidate(shared_target, source_url(settings)) == "imported"
    assert consolidate(shared_target, source_url(settings)) == "verified"
    target = database(shared_target)
    try:
        with target.begin() as conn:
            assert conn.execute(text("SELECT count(*) FROM outbox")).scalar_one() == 2
            assert conn.execute(text("SELECT nextval('outbox_sequence_seq')")).scalar_one() == 3
        # Migration 6 may advance the sequence while skipping existing events.
        # Verify transfer before migration; gaps are normal PostgreSQL behavior.
        with target.begin() as connection:
            migrate_connection(connection, version=9)
        moved = VoiceService(target, shared_target)
        assert moved.status(reference)["transcript"] == original_text
        with target.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM outbox")).scalar_one() == 2
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
    seed_legacy_recording(settings, engine)
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
