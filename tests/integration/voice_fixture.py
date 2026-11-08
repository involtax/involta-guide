import hashlib
import json
from uuid import uuid4

from sqlalchemy import text

from involta_guide.infrastructure.voice.consumer import Consumer

from .test_jobs import setup
from .test_voice_service import metadata, recording


def classified(engine, reference, category="ok"):
    """Saved successful analysis for feed fixtures; no model call or moderation approval."""
    job = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO jobs(id,kind,review_id,status) VALUES(:id,'review_analysis',:review,'done')"
            ),
            {"id": job, "review": reference},
        )
        conn.execute(
            text("""INSERT INTO review_analyses(review_id,job_id,source_text,result)
                SELECT id,:job,coalesce(transcript,''),CAST(:result AS jsonb)
                FROM reviews WHERE id=:review"""),
            {"job": job, "review": reference, "result": json.dumps({"category": category})},
        )


def imported(database):
    settings, engine, uow, queries, city, _, _ = setup(database)
    original = "Был в Огоньке сегодня, было вкусно. Телефон другой."
    reference = str(uuid4())
    from involta_voice.contracts import LocationContext

    meta = metadata().model_copy(
        update={
            "location": LocationContext(
                id=city["id"], slug=city["slug"], name=city["name"], content_epoch=1
            )
        }
    )
    event = {
        "schema_version": 2,
        "event_id": str(uuid4()),
        "sequence": 1,
        "kind": "gigaam_ready",
        "reference": reference,
        "run": 1,
        "metadata": meta.model_dump(mode="json"),
        "transcript": original,
        "analysis": None,
        "sha256": hashlib.sha256(recording()).hexdigest(),
        "mime": "audio/wav",
        "duration": 2,
        "created_at": "2026-11-03T00:00:00Z",
    }
    from involta_guide.infrastructure.voice.client import VoiceClient

    Consumer(engine, VoiceClient(settings)).import_event(event)
    return settings, engine, uow, queries, city, reference, event
