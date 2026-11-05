import argparse
import json
import time

from .config import Settings
from .db import database, migrate, row
from .main import create_app, worker_health


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=[
            "migrate",
            "worker-health",
            "openapi",
            "orphan-audio",
            "beta-cleanup-files",
            "prepare-model",
            "asr-report",
        ],
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
        elif args.command == "asr-report":
            from .recognition import summary

            print(json.dumps(summary(engine), ensure_ascii=False, default=str))
        elif args.command == "beta-cleanup-files":
            from .reset import cleanup_files
            from .service import VoiceService

            with engine.connect() as conn:
                result = row(conn, "SELECT count(*) n FROM purge_files")
                count = result["n"] if result else 0
            print(json.dumps({"pending_files": count, "execute": args.execute}))
            if args.execute:
                cleanup_files(VoiceService(engine, settings))
        else:
            # Synchronize owner-prefixed files with intake/reset and prove no committed reference.
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
                    try:
                        location = int(path.name.split(".")[0])
                    except ValueError:
                        location = None
                    if location:
                        row(
                            conn,
                            "SELECT location_id FROM location_epochs WHERE location_id=:id FOR SHARE",
                            id=location,
                        )
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
