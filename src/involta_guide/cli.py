import argparse
import getpass
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from argon2 import PasswordHasher
from sqlalchemy import inspect, text

from involta_guide.config import Settings
from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow, create_database
from involta_guide.infrastructure.seed import seed
from involta_guide.infrastructure.storage.audio import FileAudioStorage


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=["seed", "schema-audit", "hash-password", "retain-audio", "worker-health"],
    )
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.command == "hash-password":
        print(PasswordHasher().hash(getpass.getpass("Password: ")))
        return
    settings = Settings()
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
        else:
            storage = FileAudioStorage(settings.data_dir, settings.max_audio_bytes)
            cutoff = datetime.now(UTC) - timedelta(days=settings.audio_retention_days)
            with PostgresUow(engine) as uow:
                active = {
                    row["review_id"]
                    for row in uow.repo.rows("jobs", limit=100000)
                    if row["status"] in ("queued", "running")
                }
                reviews = [
                    row
                    for row in uow.repo.rows("reviews", limit=100000)
                    if row["created_at"] < cutoff
                    and row["status"] != "pending"
                    and row["id"] not in active
                    and not row["audio"].startswith("voice:")
                ]
                print(
                    f"Audio eligible: {len(reviews)}; mode: {'execute' if args.execute else 'dry-run'}"
                )
                if args.execute:
                    for row in reviews:
                        storage.delete(row["audio"])
                        uow.repo.change("reviews", row["id"], {"deleted_at": datetime.now(UTC)})
                        uow.repo.audit("audio_deleted", row["id"], "Retention policy")
    finally:
        engine.dispose()
