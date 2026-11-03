import argparse
import json
import time

from .config import Settings
from .db import database, migrate, row
from .main import create_app, worker_health


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=["migrate", "worker-health", "openapi", "orphan-audio", "prepare-model"]
    )
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.command == "worker-health":
        worker_health()
        return
    settings = Settings()
    if args.command == "openapi":
        print(json.dumps(create_app(settings).openapi(), ensure_ascii=False, indent=2))
        return
    if args.command == "prepare-model":
        from .asr import prepare

        print(prepare(settings))
        return
    engine = database(settings)
    try:
        if args.command == "migrate":
            migrate(engine)
        else:
            # Intake holds this lock until commit; never remove a referenced/young file.
            from .db import execute

            with engine.begin() as conn:
                execute(conn, "SELECT pg_advisory_xact_lock(47032002)")
                count = 0
                for path in (settings.data_dir / "audio").glob("*"):
                    if (
                        not path.is_file()
                        or path.is_symlink()
                        or time.time() - path.stat().st_mtime < 86400
                    ):
                        continue
                    if row(conn, "SELECT 1 FROM submissions WHERE audio=:name", name=path.name):
                        continue
                    count += 1
                    if args.execute:
                        path.unlink()
                print(
                    json.dumps(
                        {"orphan_files": count, "mode": "execute" if args.execute else "dry-run"}
                    )
                )
    finally:
        engine.dispose()
