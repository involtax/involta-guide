"""Private snapshot of the Voice schema before the incompatible v6 cutover.

All writers must be stopped. The Guide schema is never included or restored here.
"""

import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import text

from involta_guide.config import Settings as GuideSettings
from involta_guide.infrastructure.persistence.release_backup import run
from involta_voice.config import Settings
from involta_voice.db import database


def main() -> None:
    settings = Settings()
    operation = sys.argv[1]
    guide = GuideSettings(database_url=settings.database_url)
    if operation == "needed":
        engine = database(settings)
        try:
            with engine.connect() as conn:
                version = conn.execute(
                    text("SELECT max(version) FROM voice_schema_versions")
                ).scalar_one()
            print("yes" if version < 6 else "no")
        finally:
            engine.dispose()
        return
    with TemporaryDirectory(prefix="voice-release-") as directory:
        dump = Path(directory) / "voice.dump"
        if operation == "backup":
            run(
                [
                    "pg_dump",
                    "--format=custom",
                    "--no-owner",
                    "--no-privileges",
                    "--lock-wait-timeout=30s",
                    "--schema",
                    settings.database_schema,
                    "--file",
                    str(dump),
                ],
                guide,
            )
            run(["pg_restore", "--list", str(dump)], guide, stdout=subprocess.DEVNULL)
            with dump.open("rb") as stream:
                shutil.copyfileobj(stream, sys.stdout.buffer)
        elif operation == "restore":
            with dump.open("wb") as stream:
                shutil.copyfileobj(sys.stdin.buffer, stream)
            run(
                [
                    "pg_restore",
                    "--clean",
                    "--if-exists",
                    "--no-owner",
                    "--no-privileges",
                    "--single-transaction",
                    "--schema",
                    settings.database_schema,
                    "--dbname",
                    "",
                    str(dump),
                ],
                guide,
                stdout=subprocess.DEVNULL,
            )
            print("Voice schema restored")
        else:
            raise RuntimeError("Expected needed, backup or restore")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None
