"""Real pg_dump/cutover/pg_restore against disposable schemas and the production image.

Requires TEST_DATABASE_URL (test owner), GUIDE_TEST_IMAGE (default involta-guide:ci).
Docker uses host networking to reach the local test DB by default; override with
GUIDE_TEST_NETWORK and GUIDE_CONTAINER_DATABASE_URL for a Compose database.
No API calls, production URLs, persisted containers or database-wide DROP commands.
"""

import os
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from involta_guide.domain.rules import FACTORS

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    url = os.environ["TEST_DATABASE_URL"]
    container_url = os.getenv("GUIDE_CONTAINER_DATABASE_URL", url)
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_TLS",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
        }
    }
    docker = ["docker", "--host=unix:///var/run/docker.sock"]
    for previous in ["review_urls", "beta_core"]:
        schema = "test_release_" + uuid4().hex
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        env.update(
            DATABASE_URL=container_url,
            DATABASE_SCHEMA=schema,
            SESSION_SECRET="local-release-smoke-" + "x" * 40,
        )
        base = [
            *docker,
            "run",
            "--rm",
            "-i",
            "--network",
            os.getenv("GUIDE_TEST_NETWORK", "host"),
            "--env",
            "DATABASE_URL",
            "--env",
            "DATABASE_SCHEMA",
            "--env",
            "SESSION_SECRET",
            os.getenv("GUIDE_TEST_IMAGE", "involta-guide:ci"),
        ]

        def call(command: list[str], data: bytes | None = None) -> bytes:
            result = subprocess.run(
                [*base, *command],
                input=data,
                env=env,
                capture_output=True,
                timeout=90,
            )
            if result.returncode:
                error = result.stderr.decode(errors="replace")
                for secret in (
                    url,
                    container_url,
                    unquote(urlsplit(url).password or ""),
                    unquote(urlsplit(container_url).password or ""),
                ):
                    if secret:
                        error = error.replace(secret, "[redacted]")
                raise RuntimeError(error)
            return result.stdout

        backup_command = ["python", "-m", "involta_guide.infrastructure.persistence.release_backup"]
        try:
            call(["alembic", "upgrade", previous])
            with psycopg.connect(url, options=f"-c search_path={schema}") as conn:
                conn.execute(
                    "INSERT INTO locations(id,name,region,slug,intro) VALUES(%s,'Current city','Region','current-city','Current intro')",
                    (location_id := str(uuid4()),),
                )
                if previous == "beta_core":
                    conn.execute(
                        "INSERT INTO restaurants(id,location_id,name,cuisine,description,maps_url,position,ranking,alex_star) VALUES(%s,%s,'Restaurant','Cuisine','Description','https://yandex.ru/maps/',37,%s,true)",
                        (
                            str(uuid4()),
                            location_id,
                            Jsonb({"scores": dict.fromkeys(FACTORS, 8), "totalScore": 10}),
                        ),
                    )
                conn.execute("INSERT INTO settings VALUES('test-marker','keep')")
                conn.execute("CREATE TABLE unrelated_original(payload text)")
                conn.execute("INSERT INTO unrelated_original VALUES('before')")
            assert call([*backup_command, "needed"]).strip() == (
                b"yes" if previous == "beta_core" else b"manual-beta-transition"
            )
            dump = call([*backup_command, "backup"])
            assert dump.startswith(b"PGDMP")
            call(["alembic", "upgrade", "head"])
            assert call([*backup_command, "needed"]).strip() == b"no"
            if previous == "beta_core":
                with psycopg.connect(url, options=f"-c search_path={schema}") as conn:
                    row = conn.execute("SELECT name,ranking,alex_star FROM restaurants").fetchone()
                    assert row == ("Restaurant", {"scores": dict.fromkeys(FACTORS, 8)}, True)
                    assert (
                        conn.execute(
                            "SELECT count(*) FROM information_schema.columns WHERE table_schema=%s AND table_name='restaurants' AND column_name='position'",
                            (schema,),
                        ).fetchone()[0]
                        == 0
                    )
            # A foreign object's dependency must make restore fail atomically, never CASCADE it.
            with psycopg.connect(url, options=f"-c search_path={schema}") as conn:
                conn.execute("UPDATE unrelated_original SET payload='changed independently'")
                conn.execute(
                    "CREATE TABLE unrelated_sentinel(location_id uuid REFERENCES locations(id))"
                )
            try:
                call([*backup_command, "restore"], dump)
            except RuntimeError as exc:
                assert "depend" in str(exc)
            else:
                raise AssertionError("Restore should refuse a foreign dependency")
            with psycopg.connect(url, options=f"-c search_path={schema}") as conn:
                assert (
                    conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
                    == "review_publication"
                )
                conn.execute("DROP TABLE unrelated_sentinel")
            call([*backup_command, "restore"], dump)
            with psycopg.connect(url, options=f"-c search_path={schema}") as conn:
                assert (
                    conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
                    == previous
                )
                assert conn.execute("SELECT intro FROM locations").fetchone()[0] == "Current intro"
                if previous == "beta_core":
                    row = conn.execute(
                        "SELECT position,ranking,alex_star FROM restaurants"
                    ).fetchone()
                    assert row == (
                        37,
                        {"scores": dict.fromkeys(FACTORS, 8), "totalScore": 10},
                        True,
                    )
                assert (
                    conn.execute("SELECT value FROM settings WHERE key='test-marker'").fetchone()[0]
                    == "keep"
                )
            with psycopg.connect(url, options=f"-c search_path={schema}") as conn:
                assert (
                    conn.execute("SELECT payload FROM unrelated_original").fetchone()[0]
                    == "changed independently"
                )
            print(
                previous + ": real backup, cutover, atomic refusal and restore passed", flush=True
            )
        finally:
            with psycopg.connect(url, autocommit=True) as conn:
                conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))

    for previous in (5, 6, 7):
        voice_schema = "test_voice_restore_" + uuid4().hex
        env.update(
            VOICE_DATABASE_SCHEMA=voice_schema, VOICE_SERVICE_SECRET="local-test-" + "x" * 40
        )
        base = [
            *docker,
            "run",
            "--rm",
            "-i",
            "--network",
            os.getenv("GUIDE_TEST_NETWORK", "host"),
            "--env",
            "DATABASE_URL",
            "--env",
            "SESSION_SECRET",
            "--env",
            "VOICE_DATABASE_SCHEMA",
            "--env",
            "VOICE_SERVICE_SECRET",
            os.getenv("GUIDE_TEST_IMAGE", "involta-guide:ci"),
        ]
        backup_command = ["python", "-m", "involta_guide.infrastructure.voice.release_backup"]
        try:
            call(
                [
                    "python",
                    "-c",
                    f"from involta_voice.config import Settings; from involta_voice.db import database,migrate_connection; e=database(Settings()); c=e.connect(); t=c.begin(); migrate_connection(c,version={previous}); t.commit(); c.close()",
                ]
            )
            with psycopg.connect(url, options=f"-c search_path={voice_schema}") as conn:
                conn.execute(
                    "INSERT INTO voice_settings(key,value) VALUES('test-marker','\"keep\"'::jsonb)"
                )
            assert call([*backup_command, "needed"]).strip() == b"yes"
            dump = call([*backup_command, "backup"])
            assert dump.startswith(b"PGDMP")
            call(["voice", "migrate"])
            assert call([*backup_command, "needed"]).strip() == b"no"
            call([*backup_command, "restore"], dump)
            with psycopg.connect(url, options=f"-c search_path={voice_schema}") as conn:
                assert (
                    conn.execute("SELECT max(version) FROM voice_schema_versions").fetchone()[0]
                    == previous
                )
                restored_evaluations = conn.execute(
                    "SELECT to_regclass('asr_evaluations')"
                ).fetchone()[0]
                assert bool(restored_evaluations) == (previous == 5)
                assert (
                    conn.execute(
                        "SELECT value FROM voice_settings WHERE key='test-marker'"
                    ).fetchone()[0]
                    == "keep"
                )
            print(
                f"Voice: real shared-schema v{previous} → v8 → pg_restore v{previous} passed",
                flush=True,
            )
        finally:
            with psycopg.connect(url, autocommit=True) as conn:
                conn.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(voice_schema))
                )


if __name__ == "__main__":
    main()
