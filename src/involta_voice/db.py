import json
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, text

from .config import Settings


def database(settings: Settings) -> Engine:
    url = settings.database_url.get_secret_value().replace(
        "postgresql://", "postgresql+psycopg://", 1
    )
    return create_engine(
        url,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={
            "connect_timeout": 10,
            "options": f"-c search_path={settings.database_schema} -c statement_timeout=30000",
        },
    )


def row(conn: Connection, query: str, **params: Any) -> dict[str, Any] | None:
    result = conn.execute(text(query), params).mappings().first()
    return dict(result) if result else None


def execute(conn: Connection, query: str, **params: Any) -> None:
    conn.execute(text(query), params)


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def migrate(engine: Engine) -> None:
    with engine.begin() as conn:
        migrate_connection(conn)


def migrate_connection(conn: Connection, version: int = 6) -> None:
    # The connection's search_path selects the Voice-owned schema. No fallback
    # to public: an absent Voice table must never resolve to a Guide table.
    schema = conn.execute(text("SHOW search_path")).scalar_one()
    quoted = conn.dialect.identifier_preparer.quote(schema)
    execute(conn, "SELECT pg_advisory_xact_lock(47032001)")
    conn.exec_driver_sql(f"CREATE SCHEMA IF NOT EXISTS {quoted}")
    exists = row(conn, "SELECT to_regclass('voice_schema_versions') AS name")
    if not exists or not exists["name"]:
        conn.exec_driver_sql((Path(__file__).parent / "migrations/001_initial.sql").read_text())
    if not row(conn, "SELECT 1 FROM voice_schema_versions WHERE version=2"):
        conn.exec_driver_sql(
            (Path(__file__).parent / "migrations/002_guide_transcription.sql").read_text()
        )
    if not row(conn, "SELECT 1 FROM voice_schema_versions WHERE version=3"):
        conn.exec_driver_sql((Path(__file__).parent / "migrations/003_asr_beta.sql").read_text())
    if not row(conn, "SELECT 1 FROM voice_schema_versions WHERE version=4"):
        conn.exec_driver_sql((Path(__file__).parent / "migrations/004_beta_core.sql").read_text())
    if not row(conn, "SELECT 1 FROM voice_schema_versions WHERE version=5"):
        conn.exec_driver_sql((Path(__file__).parent / "migrations/005_no_money.sql").read_text())

    if version >= 6 and not row(conn, "SELECT 1 FROM voice_schema_versions WHERE version=6"):
        conn.exec_driver_sql(
            (Path(__file__).parent / "migrations/006_review_stream.sql").read_text()
        )


def snapshot(conn: Connection, settings: Settings) -> dict[str, Any]:
    from .asr import MODELS, options

    selected = "gigaam"

    return {
        "transcription": options(settings.model_copy(update={"transcription_model": selected})),
        "asr_models": {e: options(settings, e) for e in MODELS},
        "asr_beta": settings.asr_beta_enabled,
        "fake_provider": settings.fake_provider,
        "schema_version": 2,
        "transcription_model": selected,
    }
