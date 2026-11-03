"""Private, transactionally restorable schema backup for the one-time architecture cutover.

Used by the deployment controller with all Guide writers stopped. Secrets stay in
libpq's environment; stdout contains only the dump (backup) or a public status.
"""

import os
import re
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urlsplit

from psycopg.conninfo import conninfo_to_dict
from psycopg.pq import Conninfo
from sqlalchemy import inspect

from involta_guide.config import Settings
from involta_guide.infrastructure.persistence.unit_of_work import create_database


def pg_environment(settings: Settings) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    names = {
        option.keyword.decode(): option.envvar.decode()
        for option in Conninfo.get_defaults()
        if option.envvar
    }
    for key, value in conninfo_to_dict(settings.database_url.get_secret_value()).items():
        if key not in names:
            raise RuntimeError("Connection option has no safe libpq environment equivalent: " + key)
        if value is not None:
            env[names[key]] = str(value)
    env["PGCONNECT_TIMEOUT"] = "15"
    return env


def run(
    args: list[str], settings: Settings, **kwargs: object
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(args, env=pg_environment(settings), stderr=subprocess.PIPE, **kwargs)  # type: ignore[call-overload]
    if result.returncode:
        message = result.stderr.decode(errors="replace")
        url = settings.database_url.get_secret_value()
        for value in (url, unquote(urlsplit(url).password or "")):
            if value:
                message = message.replace(value, "[redacted]")
        raise RuntimeError(message.strip() or "PostgreSQL backup/restore failed")
    return result


def owned_tables(settings: Settings) -> set[str]:
    return set(
        re.findall(
            r"CREATE TABLE (\w+)", (settings.root_dir / "migrations/current_schema.sql").read_text()
        )
    ) | {"alembic_version", "beta_cleanup_files", "beta_transition_report", "ai_daily_usage"}


def main() -> None:
    settings = Settings()
    mode = sys.argv[1]
    if mode in ("needed", "compatibility"):
        engine = create_database(settings)
        try:
            with engine.connect() as conn:
                tables = set(inspect(conn).get_table_names(schema=settings.database_schema))
                beta = "beta_transition_report" in tables
                if mode == "compatibility":
                    print("beta" if beta else "previous" if "locations" in tables else "empty")
                else:
                    print("manual-beta-transition" if "locations" in tables and not beta else "no")
        finally:
            engine.dispose()
        return
    with TemporaryDirectory(prefix="guide-release-") as directory:
        dump = Path(directory) / "guide.dump"
        if mode == "backup":
            engine = create_database(settings)
            try:
                with engine.connect() as conn:
                    selected = set(
                        inspect(conn).get_table_names(schema=settings.database_schema)
                    ) & owned_tables(settings)
                    quote = engine.dialect.identifier_preparer.quote_identifier
                    table_args = [
                        arg
                        for table in sorted(selected)
                        for arg in ("--table", quote(settings.database_schema) + "." + quote(table))
                    ]
            finally:
                engine.dispose()
            if not {"locations", "restaurants", "alembic_version"} <= selected:
                raise RuntimeError("Guide tables missing; backup refused")
            run(
                [
                    "pg_dump",
                    "--format=custom",
                    "--no-owner",
                    "--no-privileges",
                    "--lock-wait-timeout=30s",
                    *table_args,
                    "--file",
                    str(dump),
                ],
                settings,
            )
            run(["pg_restore", "--list", str(dump)], settings, stdout=subprocess.DEVNULL)
            with dump.open("rb") as stream:
                import shutil

                shutil.copyfileobj(stream, sys.stdout.buffer)
            return
        if mode != "restore":
            raise RuntimeError("Expected needed, backup or restore")
        with dump.open("wb") as stream:
            import shutil

            shutil.copyfileobj(sys.stdin.buffer, stream)
        listing = run(
            ["pg_restore", "--list", str(dump)], settings, stdout=subprocess.PIPE
        ).stdout.decode()
        archived = set(re.findall(r"; \d+ \d+ TABLE \S+ (\w+) \S+$", listing, re.MULTILINE))
        if not {
            "locations",
            "restaurants",
            "alembic_version",
        } <= archived or not archived <= owned_tables(settings):
            raise RuntimeError("Not a Guide schema backup; restore refused")
        toc = Path(directory) / "restore.list"
        toc.write_text(
            "\n".join(
                line
                for line in listing.splitlines()
                if not re.search(r"; \d+ \d+ (?:SCHEMA |COMMENT - SCHEMA )", line)
            )
            + "\n"
        )
        sql_file = Path(directory) / "restore.sql"
        run(
            [
                "pg_restore",
                "--use-list",
                str(toc),
                "--clean",
                "--if-exists",
                "--no-owner",
                "--no-privileges",
                "--file",
                str(sql_file),
                str(dump),
            ],
            settings,
        )
        engine = create_database(settings)
        try:
            with engine.connect() as conn:
                present = set(inspect(conn).get_table_names(schema=settings.database_schema))
                owned = owned_tables(settings)
                added = (present & owned) - archived
                quote = engine.dialect.identifier_preparer.quote_identifier
                # Remove owned FKs before pg_restore drops referenced primary keys.
                # All statements below share psql's single transaction with restore.
                prefix = ""
                inspector = inspect(conn)
                for table in sorted(present & owned):
                    for foreign in inspector.get_foreign_keys(
                        table, schema=settings.database_schema
                    ):
                        assert foreign["name"] is not None
                        prefix += (
                            "ALTER TABLE "
                            + quote(settings.database_schema)
                            + "."
                            + quote(table)
                            + " DROP CONSTRAINT "
                            + quote(foreign["name"])
                            + ";\n"
                        )
                if added:
                    prefix += (
                        "DROP TABLE "
                        + ",".join(
                            quote(settings.database_schema) + "." + quote(t) for t in sorted(added)
                        )
                        + ";\n"
                    )
            # Drop only Guide tables absent in the backup. No DROP SCHEMA / CASCADE.
            sql_file.write_text(prefix + sql_file.read_text())
            run(
                [
                    "psql",
                    "--no-psqlrc",
                    "--single-transaction",
                    "--set",
                    "ON_ERROR_STOP=1",
                    "--file",
                    str(sql_file),
                ],
                settings,
                stdout=subprocess.DEVNULL,
            )
        finally:
            engine.dispose()
        print("Guide schema and data restored")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
