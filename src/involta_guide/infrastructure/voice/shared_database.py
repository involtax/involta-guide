"""One-time, transactional move from the retired Voice database into schema voice.

Run only with Guide/Voice writers stopped. Audio stays in its existing volume.
The source database is never modified. No credentials or row content are logged.
"""

import hashlib
import os
from typing import Any, cast

import psycopg
from psycopg import sql
from sqlalchemy import inspect, text

from involta_voice.config import Settings
from involta_voice.db import database, migrate_connection


def digest(connection: psycopg.Connection[Any], schema: str, table: str) -> bytes:
    checksum = hashlib.sha256()
    query = sql.SQL(
        "COPY (SELECT row_to_json(t)::text FROM {}.{} t "
        'ORDER BY row_to_json(t)::text COLLATE "C") TO STDOUT'
    ).format(sql.Identifier(schema), sql.Identifier(table))
    with connection.cursor().copy(query) as stream:
        for chunk in stream:
            checksum.update(chunk)
    return checksum.digest()


def consolidate(settings: Settings, source_url: str | None) -> str:
    engine = database(settings)
    try:
        with engine.begin() as target:
            # Serialize retries; no pre-existing target data is overwritten.
            target.execute(text("SELECT pg_advisory_xact_lock(47032003)"))
            if not source_url:
                migrate_connection(target)
                return "new"
            with psycopg.connect(source_url, connect_timeout=10) as source:
                source.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                origin = source.execute(
                    "SELECT current_database(),inet_server_addr()::text,inet_server_port(),current_schema()"
                ).fetchone()
                assert origin is not None
                destination = target.execute(
                    text("SELECT current_database(),inet_server_addr()::text,inet_server_port()")
                ).one()
                if origin[:3] == tuple(destination):
                    if origin[3] != settings.database_schema:
                        raise RuntimeError("Shared database configured with the wrong Voice schema")
                    migrate_connection(target)
                    return "shared"
                existed = (
                    target.execute(
                        text("SELECT 1 FROM pg_namespace WHERE nspname=:schema"),
                        {"schema": settings.database_schema},
                    ).first()
                    is not None
                )
                version = source.execute(
                    "SELECT max(version) FROM voice_schema_versions"
                ).fetchone()
                if not version or version[0] != 5:
                    raise RuntimeError("Expected Voice schema version 5 before consolidation")
                source_schema = origin[3]
                source_tables = {
                    r[0]
                    for r in source.execute(
                        "SELECT tablename FROM pg_tables WHERE schemaname=%s", (source_schema,)
                    )
                }
                if not existed:
                    migrate_connection(target)
                inspector = inspect(target)
                tables = inspector.get_table_names(schema=settings.database_schema)
                if set(tables) != source_tables:
                    raise RuntimeError("Source/target Voice table sets differ; migration refused")
                raw = cast(psycopg.Connection[Any], target.connection.driver_connection)
                schema = settings.database_schema
                if existed:
                    # Recover a crash after COPY commit but before the private env
                    # switch. Compare, never overwrite an existing destination.
                    for table in tables:
                        if digest(source, source_schema, table) != digest(raw, schema, table):
                            raise RuntimeError(
                                "Existing Voice schema differs from source; overwrite refused"
                            )
                    return "verified"
                names = sql.SQL(",").join(sql.Identifier(schema, name) for name in tables)
                raw.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY").format(names))
                for table in tables:
                    raw.execute(
                        sql.SQL("ALTER TABLE {}.{} DISABLE TRIGGER USER").format(
                            sql.Identifier(schema), sql.Identifier(table)
                        )
                    )
                for ordered_table, _ in inspector.get_sorted_table_and_fkc_names(schema=schema):
                    if ordered_table is None:
                        continue
                    table = ordered_table
                    columns = [c["name"] for c in inspector.get_columns(table, schema=schema)]
                    fields = sql.SQL(",").join(map(sql.Identifier, columns))
                    read = sql.SQL("COPY {}.{} ({}) TO STDOUT WITH (FORMAT BINARY)").format(
                        sql.Identifier(source_schema), sql.Identifier(table), fields
                    )
                    write = sql.SQL("COPY {}.{} ({}) FROM STDIN WITH (FORMAT BINARY)").format(
                        sql.Identifier(schema), sql.Identifier(table), fields
                    )
                    with (
                        source.cursor().copy(read) as incoming,
                        raw.cursor().copy(write) as outgoing,
                    ):
                        for chunk in incoming:
                            outgoing.write(chunk)
                for table in tables:
                    if digest(source, source_schema, table) != digest(raw, schema, table):
                        raise RuntimeError("Voice data verification failed: " + table)
                    raw.execute(
                        sql.SQL("ALTER TABLE {}.{} ENABLE TRIGGER USER").format(
                            sql.Identifier(schema), sql.Identifier(table)
                        )
                    )
                for (name,) in source.execute(
                    "SELECT sequencename FROM pg_sequences WHERE schemaname=%s", (source_schema,)
                ).fetchall():
                    sequence_state = source.execute(
                        sql.SQL("SELECT last_value,is_called FROM {}.{}").format(
                            sql.Identifier(source_schema), sql.Identifier(name)
                        )
                    ).fetchone()
                    assert sequence_state is not None
                    value, called = sequence_state
                    raw.execute(
                        "SELECT setval(%s::regclass,%s,%s)", (schema + "." + name, value, called)
                    )
                return "imported"
    finally:
        engine.dispose()


def main() -> None:
    try:
        result = consolidate(Settings(), os.environ.get("VOICE_DATABASE_URL"))
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None
    except Exception as exc:
        # Database exceptions can contain connection credentials or row content.
        raise SystemExit(
            "Voice database consolidation failed (" + type(exc).__name__ + "); source retained"
        ) from None
    print("Voice database: " + result)


if __name__ == "__main__":
    main()
