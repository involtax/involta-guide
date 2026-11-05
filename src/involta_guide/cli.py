import argparse
import getpass
import json
from typing import Any

from argon2 import PasswordHasher
from sqlalchemy import inspect, text

from involta_guide.config import Settings
from involta_guide.infrastructure.location_catalog import export_catalog
from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow, create_database
from involta_guide.infrastructure.seed import seed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=[
            "seed",
            "export-locations",
            "schema-audit",
            "hash-password",
            "beta-cleanup-files",
            "worker-health",
            "voice-consumer-health",
        ],
    )
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.command == "hash-password":
        print(PasswordHasher().hash(getpass.getpass("Password: ")))
        return
    settings = Settings()
    if args.command == "voice-consumer-health":
        from involta_guide.infrastructure.voice.diagnostics import health

        health()
        return
    if args.command == "worker-health":
        import time

        path = settings.worker_heartbeat_file
        raise SystemExit(
            0
            if path.exists() and time.time() - path.stat().st_mtime < settings.lease_seconds + 30
            else 1
        )
    engine = create_database(settings)
    try:
        if args.command == "seed":
            print(f"Locations added: {seed(PostgresUow(engine), settings.root_dir)}")
        elif args.command == "export-locations":
            with PostgresUow(engine) as work:
                catalog = export_catalog(
                    settings.root_dir,
                    work.repo.rows("locations", limit=100000),
                    work.repo.rows("location_slug_redirects", limit=100000),
                )
            print(json.dumps(catalog, ensure_ascii=False, indent=2))
        elif args.command == "schema-audit":
            inspector = inspect(engine)
            result: dict[str, Any] = {}
            with engine.connect() as conn:
                for name in inspector.get_table_names(schema=settings.database_schema):
                    # Identifiers originate from the PostgreSQL inspector, not user input.
                    quoted = engine.dialect.identifier_preparer.quote(name)
                    count = conn.execute(text(f"SELECT count(*) FROM {quoted}")).scalar()
                    result[name] = {
                        "count": count,
                        "foreign_keys": len(
                            inspector.get_foreign_keys(name, schema=settings.database_schema)
                        ),
                        "indexes": len(
                            inspector.get_indexes(name, schema=settings.database_schema)
                        ),
                    }
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "beta-cleanup-files":
            from involta_guide.infrastructure.beta_reset import cleanup_files

            with engine.connect() as conn:
                count = conn.execute(text("SELECT count(*) FROM beta_cleanup_files")).scalar()
            print(f"Files pending: {count}; mode: {'execute' if args.execute else 'dry-run'}")
            if args.execute:
                cleanup_files(engine, settings)
    finally:
        engine.dispose()
