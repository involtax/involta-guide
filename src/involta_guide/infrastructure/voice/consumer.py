import time
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Engine

from involta_guide.config import Settings
from involta_guide.infrastructure.persistence.unit_of_work import create_database
from involta_voice.contracts import OutboxEvent, SubmissionMetadata, validate_analysis
from involta_voice.db import encoded, execute, row

from .client import VoiceClient


class Consumer:
    def __init__(self, engine: Engine, client: VoiceClient) -> None:
        self.engine, self.client = engine, client

    def once(self) -> int:
        with self.engine.connect() as conn:
            cursor = row(conn, "SELECT sequence FROM voice_cursor WHERE consumer='voice'")
        events = self.client.json(
            "GET", "/events", params={"after": cursor["sequence"] if cursor else 0}
        )["events"]
        imported = 0
        for event in events:
            imported += self.import_event(event)
        # Ack only after Guide's transaction has committed. Lost ack is harmless.
        with self.engine.connect() as conn:
            committed = row(conn, "SELECT sequence FROM voice_cursor WHERE consumer='voice'")
        if committed:
            self.client.json("POST", "/events/ack", json={"sequence": committed["sequence"]})
        return imported

    def import_event(self, event: dict[str, Any]) -> int:
        event = OutboxEvent.model_validate(event).model_dump(mode="json")
        if event.get("schema_version") != 1 or event.get("kind") not in (
            "transcript_ready",
            "analysis_ready",
            "processing_failed",
        ):
            raise ValueError("Unsupported Voice event schema")
        metadata = SubmissionMetadata.model_validate(event["metadata"])
        reference, event_id = str(UUID(event["reference"])), str(UUID(event["event_id"]))
        number = int(event["run"])
        sequence = int(event["sequence"])
        transcript = event["transcript"]
        if (
            number < 1
            or sequence < 1
            or not isinstance(transcript, str)
            or not transcript.strip()
            or len(transcript) > 30000
        ):
            raise ValueError("Invalid transcript event")
        analysis = (
            validate_analysis(event["analysis"], transcript, metadata.location)
            if event["kind"] == "analysis_ready"
            else None
        )
        with self.engine.begin() as conn:
            execute(conn, "SELECT pg_advisory_xact_lock(47033001)")
            if row(conn, "SELECT 1 FROM voice_inbox WHERE event_id=:id", id=event_id):
                return 0
            location = row(
                conn,
                "SELECT id FROM locations WHERE id=:id FOR UPDATE",
                id=str(metadata.location.id),
            )
            if location is None:
                raise ValueError("Location identity no longer exists in Guide")
            link = row(
                conn, "SELECT * FROM voice_links WHERE reference=:id FOR UPDATE", id=reference
            )
            if link is None:
                ordinal = row(
                    conn,
                    "SELECT coalesce(max(number),0)+1 n FROM voice_links WHERE location_id=:id",
                    id=str(metadata.location.id),
                )
                execute(
                    conn,
                    """INSERT INTO reviews(id,location_id,visitor,audio,mime,hash,ip_hash,device,
                    transcript,status,duration_seconds,result) VALUES(:id,:location,:visitor,:audio,
                    :mime,:sha,:ip,:agent,:transcript,'processed',:duration,'{"entries":[],"suggestions":[]}'::jsonb)""",
                    id=reference,
                    location=str(metadata.location.id),
                    visitor=metadata.visitor,
                    audio="voice:" + reference,
                    mime=event["mime"],
                    sha=event["sha256"],
                    ip=metadata.ip_fingerprint,
                    agent=metadata.user_agent,
                    transcript=transcript,
                    duration=event["duration"],
                )
                execute(
                    conn,
                    "INSERT INTO review_texts(review_id,original_text,editorial_text) VALUES(:id,:text,:text)",
                    id=reference,
                    text=transcript,
                )
                execute(
                    conn,
                    """INSERT INTO voice_links(review_id,reference,location_id,number,active_run,metadata)
                    VALUES(:id,:id,:location,:number,:run,CAST(:metadata AS jsonb))""",
                    id=reference,
                    location=str(metadata.location.id),
                    number=ordinal["n"] if ordinal else 1,
                    run=number,
                    metadata=encoded(
                        {
                            **metadata.model_dump(mode="json"),
                            "repeat_signals": event.get("repeat_signals", {}),
                        }
                    ),
                )
                link = {"review_id": reference, "active_run": number}
            original = row(
                conn, "SELECT original_text FROM review_texts WHERE review_id=:id", id=reference
            )
            if not original or original["original_text"] != transcript:
                raise ValueError("Original transcript cannot be replaced by a later event")
            if analysis is not None:
                execute(
                    conn,
                    "INSERT INTO voice_results(review_id,run,analysis) VALUES(:id,:run,CAST(:result AS jsonb)) ON CONFLICT DO NOTHING",
                    id=reference,
                    run=number,
                    result=analysis.model_dump_json(),
                )
                for index, fragment in enumerate(analysis.entries):
                    execute(
                        conn,
                        "INSERT INTO voice_fragments(review_id,run,number,fragment) VALUES(:id,:run,:n,CAST(:value AS jsonb)) ON CONFLICT DO NOTHING",
                        id=reference,
                        run=number,
                        n=index,
                        value=fragment.model_dump_json(),
                    )
                for index, suggestion in enumerate(analysis.suggestions):
                    if row(
                        conn,
                        "SELECT 1 FROM voice_proposals WHERE review_id=:id AND run=:run AND number=:n",
                        id=reference,
                        run=number,
                        n=index,
                    ):
                        continue
                    proposal = str(uuid4())
                    payload = {**suggestion.changes, **suggestion.model_dump(mode="json")}
                    execute(
                        conn,
                        "INSERT INTO proposals(id,location_id,review_id,kind,payload) VALUES(:p,:location,:id,:kind,CAST(:payload AS jsonb))",
                        p=proposal,
                        location=str(metadata.location.id),
                        id=reference,
                        kind=suggestion.kind,
                        payload=encoded(payload),
                    )
                    execute(
                        conn,
                        "INSERT INTO voice_proposals(proposal_id,review_id,run,number) VALUES(:p,:id,:run,:n)",
                        p=proposal,
                        id=reference,
                        run=number,
                        n=index,
                    )
                # New runs are candidates. Already accepted decisions/attributes are never overwritten.
                if link["active_run"] == number:
                    execute(conn, "UPDATE reviews SET error=NULL WHERE id=:id", id=reference)
                    execute(
                        conn,
                        "UPDATE reviews SET result=CAST(:result AS jsonb) WHERE id=:id AND moderation='pending'",
                        result=analysis.model_dump_json(),
                        id=reference,
                    )
                    ids = {
                        str(e.restaurantId) for e in analysis.entries if e.restaurantId is not None
                    }
                    if len(ids) == 1 and all(e.restaurantId is not None for e in analysis.entries):
                        execute(
                            conn,
                            "UPDATE review_texts SET restaurant_id=:restaurant WHERE review_id=:id AND restaurant_id IS NULL AND revision=1",
                            restaurant=next(iter(ids)),
                            id=reference,
                        )
            elif event["kind"] == "processing_failed" and link["active_run"] == number:
                execute(
                    conn,
                    "UPDATE reviews SET error=:error WHERE id=:id",
                    error=str(event["analysis"].get("error", "Ошибка анализа"))[:2000],
                    id=reference,
                )
            execute(
                conn,
                "INSERT INTO voice_inbox(event_id,sequence,review_id,run,kind) VALUES(:event,:seq,:id,:run,:kind)",
                event=event_id,
                seq=sequence,
                id=reference,
                run=number,
                kind=event["kind"],
            )
            execute(
                conn,
                "UPDATE voice_cursor SET sequence=greatest(sequence,:seq) WHERE consumer='voice'",
                seq=sequence,
            )
        return 1


def main() -> None:
    settings = Settings()
    engine = create_database(settings)
    consumer = Consumer(engine, VoiceClient(settings))
    try:
        while True:
            try:
                worked = consumer.once()
            except Exception:
                worked = 0
            if not worked:
                time.sleep(3)
    finally:
        engine.dispose()
