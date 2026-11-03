import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from involta_voice.db import encoded, execute, row

PATH = Path(os.getenv("VOICE_CONSUMER_STATE", "/tmp/involta-guide-voice-consumer.json"))


def state(engine: Engine | None = None, **values: Any) -> None:
    previous = json.loads(PATH.read_text()) if PATH.exists() else {}
    previous.update(values, heartbeat=time.time())
    if engine is not None:
        with engine.begin() as conn:
            existing = row(conn, "SELECT state FROM voice_consumer_health WHERE id=1")
            stored = existing["state"] if existing else {}
            stored.update(values, heartbeat=time.time())
            execute(
                conn,
                "INSERT INTO voice_consumer_health(id,state) VALUES(1,CAST(:state AS jsonb)) ON CONFLICT(id) DO UPDATE SET state=excluded.state",
                state=encoded(stored),
            )
    temporary = PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(previous))
    temporary.replace(PATH)


def health() -> None:
    from involta_guide.config import Settings
    from involta_guide.infrastructure.persistence.unit_of_work import create_database

    engine = create_database(Settings())
    try:
        with engine.connect() as conn:
            result = row(conn, "SELECT state FROM voice_consumer_health WHERE id=1")
        current = result["state"] if result else {}
        if time.time() - current.get("heartbeat", 0) > 120 or current.get("error_type"):
            raise SystemExit(1)
    finally:
        engine.dispose()


def failure(
    exc: Exception, event: dict[str, Any] | None = None, engine: Engine | None = None
) -> None:
    identity = (
        {k: event.get(k) for k in ("event_id", "sequence", "reference", "run")} if event else {}
    )
    # Exception messages/locals can contain untrusted payloads or credentials.
    # Stack frames retain diagnostic locations without rendering exception values.
    import traceback

    frames = traceback.extract_tb(exc.__traceback__)
    diagnostic = {
        "error_type": type(exc).__name__,
        **identity,
        "stack": [{"file": f.filename, "line": f.lineno, "function": f.name} for f in frames],
    }
    logging.getLogger("voice.consumer").error(json.dumps(diagnostic))
    state(
        engine, **diagnostic, last_attempt=time.time(), status="blocked" if event else "unavailable"
    )
