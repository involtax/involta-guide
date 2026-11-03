import time
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import Engine
from sqlalchemy import text as sql_text

from involta_guide.config import Settings
from involta_guide.infrastructure.persistence.unit_of_work import create_database
from involta_voice.contracts import OutboxEvent, SubmissionMetadata, validate_analysis
from involta_voice.db import encoded, execute, row

from .client import VoiceClient


class Consumer:
    def __init__(self, engine: Engine, client: VoiceClient) -> None:
        self.engine, self.client = engine, client

    def once(self) -> int:
        try:
            return self._once()
        except Exception as exc:
            if not getattr(exc, "voice_diagnosed", False):
                from .diagnostics import failure

                failure(exc, engine=self.engine)
            raise

    def _once(self) -> int:
        from .diagnostics import state

        with self.engine.connect() as conn:
            cursor = row(conn, "SELECT sequence FROM voice_cursor WHERE consumer='voice'")
        events = self.client.json(
            "GET", "/events", params={"after": cursor["sequence"] if cursor else 0}
        )["events"]
        imported = 0
        for event in events:
            # Receipt must commit before a later event can advance the global cursor.
            # A database failure here aborts the batch and leaves the event unacknowledged.
            if self._receive(event):
                try:
                    imported += self._apply(event)
                except Exception as exc:
                    self._failed(event, exc)
        with self.engine.connect() as conn:
            pending = (
                conn.execute(
                    sql_text(
                        "SELECT payload FROM voice_inbox WHERE state='pending' ORDER BY sequence LIMIT 100"
                    )
                )
                .scalars()
                .all()
            )
        for event in pending:
            try:
                imported += self._apply(event)
            except Exception as exc:
                self._failed(event, exc)
        with self.engine.connect() as conn:
            committed = row(conn, "SELECT sequence FROM voice_cursor WHERE consumer='voice'")
            failed = row(conn, "SELECT count(*) n FROM voice_inbox WHERE state='failed'")
        if committed:
            self.client.json("POST", "/events/ack", json={"sequence": committed["sequence"]})
        state(
            self.engine,
            error_type=None,
            status="progress" if imported else "idle",
            last_success=time.time(),
            failed_deliveries=failed["n"] if failed else 0,
            cursor=committed["sequence"] if committed else 0,
        )
        return imported

    @staticmethod
    def _event_id(event: dict[str, Any]) -> str:
        try:
            return str(UUID(event["event_id"]))
        except (KeyError, ValueError, TypeError, AttributeError):
            return str(uuid5(NAMESPACE_URL, "voice-invalid-event:" + str(event["sequence"])))

    def _receive(self, event: dict[str, Any]) -> bool:
        identity = self._event_id(event)
        sequence = int(event["sequence"])
        if sequence < 1:
            raise ValueError("Invalid event sequence")
        try:
            reference: str | None = str(UUID(event["reference"]))
        except (KeyError, ValueError, TypeError, AttributeError):
            reference = None
        try:
            context = event["metadata"]["location"]
            location_id: str | None = str(UUID(context["id"]))
            epoch = int(context["content_epoch"])
        except (KeyError, ValueError, TypeError, AttributeError):
            location_id, epoch = None, 0
        try:
            run = int(event["run"])
        except (KeyError, ValueError, TypeError):
            run = 0

        with self.engine.begin() as conn:
            location = row(
                conn, "SELECT content_epoch FROM locations WHERE id=:id FOR UPDATE", id=location_id
            )
            # The epoch is durable proof that the old event is intentionally ignored.
            stale = bool(location and epoch < location["content_epoch"])
            if not stale:
                execute(
                    conn,
                    """INSERT INTO voice_inbox(event_id,sequence,review_id,run,kind,location_id,content_epoch,payload,state)
                    VALUES(:id,:seq,:ref,:run,:kind,:loc,:epoch,CAST(:payload AS jsonb),'pending') ON CONFLICT DO NOTHING""",
                    id=identity,
                    seq=sequence,
                    ref=reference,
                    run=run,
                    kind=str(event.get("kind", "invalid")),
                    loc=location_id if location else None,
                    epoch=epoch,
                    payload=encoded(event),
                )
            execute(
                conn,
                "UPDATE voice_cursor SET sequence=greatest(sequence,:seq) WHERE consumer='voice'",
                seq=sequence,
            )
        return not stale

    def _failed(self, event: dict[str, Any], exc: Exception) -> None:
        with self.engine.begin() as conn:
            execute(
                conn,
                "UPDATE voice_inbox SET state='failed',error=:error WHERE event_id=:id AND state<>'imported'",
                error=type(exc).__name__
                + ": не удалось применить событие; вход сохранён для повтора",
                id=self._event_id(event),
            )

    def retry(self, event_id: str) -> int:
        with self.engine.begin() as conn:
            event = row(
                conn,
                "UPDATE voice_inbox SET state='pending',error=NULL WHERE event_id=:id RETURNING payload",
                id=event_id,
            )
        if not event:
            raise ValueError("Событие не найдено")
        try:
            return self._apply(event["payload"])
        except Exception as exc:
            self._failed(event["payload"], exc)
            raise ValueError("Событие снова не применено; причина сохранена") from exc

    def import_event(self, event: dict[str, Any]) -> int:
        if not self._receive(event):
            return 0
        try:
            return self._apply(event)
        except Exception as exc:
            self._failed(event, exc)
            raise

    def _apply(self, event: dict[str, Any]) -> int:
        event = OutboxEvent.model_validate(event).model_dump(mode="json")
        if event.get("schema_version") != 2 or event.get("kind") not in (
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
            location = row(
                conn,
                "SELECT * FROM locations WHERE id=:id FOR UPDATE",
                id=str(metadata.location.id),
            )
            if location is None:
                raise ValueError("Location identity no longer exists in Guide")
            inbox = row(
                conn, "SELECT state FROM voice_inbox WHERE event_id=:id FOR UPDATE", id=event_id
            )
            if not inbox or inbox["state"] == "imported":
                return 0
            if metadata.location.content_epoch < location["content_epoch"]:
                execute(conn, "DELETE FROM voice_inbox WHERE event_id=:id", id=event_id)
                return 0
            if (
                location["reset_pending"]
                or location["is_hub"]
                or metadata.location.content_epoch != location["content_epoch"]
            ):
                raise ValueError("Локация не принимает отзыв в этом цикле")
            earlier = row(
                conn,
                "SELECT 1 FROM voice_inbox WHERE review_id=:id AND sequence<:seq AND state<>'imported'",
                id=reference,
                seq=sequence,
            )
            if earlier:
                return 0
            if analysis:
                ids = {str(e.restaurantId) for e in analysis.entries if e.restaurantId}
                ids.update(str(e.restaurantId) for e in analysis.suggestions if e.restaurantId)
                ids.update(str(c) for e in analysis.entries for c in e.candidates)
                for identity in ids:
                    if not row(
                        conn,
                        "SELECT 1 FROM restaurants WHERE id=:id AND location_id=:loc",
                        id=identity,
                        loc=str(metadata.location.id),
                    ):
                        raise ValueError("Чужой ресторан в обработке")
            link = row(
                conn, "SELECT * FROM voice_links WHERE reference=:id FOR UPDATE", id=reference
            )
            if link is None:
                ordinal = row(
                    conn,
                    "UPDATE locations SET next_review_number=next_review_number+1 WHERE id=:id RETURNING next_review_number n",
                    id=str(metadata.location.id),
                )
                execute(
                    conn,
                    """INSERT INTO reviews(id,location_id,content_epoch,visitor,audio,mime,hash,ip,user_agent,
                    transcript,status,duration_seconds,result) VALUES(:id,:location,:epoch,:visitor,:audio,
                    :mime,:sha,:ip,:agent,:transcript,'processed',:duration,'{"entries":[],"suggestions":[]}'::jsonb)""",
                    id=reference,
                    location=str(metadata.location.id),
                    epoch=metadata.location.content_epoch,
                    visitor=metadata.visitor,
                    audio="voice:" + reference,
                    mime=event["mime"],
                    sha=event["sha256"],
                    ip=metadata.ip,
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
                        "INSERT INTO voice_fragments(review_id,run,number,restaurant_id,fragment) VALUES(:id,:run,:n,:restaurant,CAST(:value AS jsonb)) ON CONFLICT DO NOTHING",
                        id=reference,
                        run=number,
                        n=index,
                        restaurant=str(fragment.restaurantId) if fragment.restaurantId else None,
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
                        "INSERT INTO proposals(id,location_id,review_id,kind,restaurant_id,payload) VALUES(:p,:location,:id,:kind,:restaurant,CAST(:payload AS jsonb))",
                        p=proposal,
                        location=str(metadata.location.id),
                        id=reference,
                        kind=suggestion.kind,
                        restaurant=str(suggestion.restaurantId)
                        if suggestion.restaurantId
                        else None,
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
                execute(
                    conn,
                    "UPDATE review_texts SET revision=revision+1 WHERE review_id=:id AND EXISTS (SELECT 1 FROM reviews WHERE id=:id AND moderation='pending')",
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
                "UPDATE voice_inbox SET state='imported',error=NULL WHERE event_id=:id",
                id=event_id,
            )
        return 1


def main() -> None:
    settings = Settings()
    engine = create_database(settings)
    consumer = Consumer(engine, VoiceClient(settings))
    backoff = 3
    try:
        while True:
            try:
                worked = consumer.once()
                backoff = 3
            except Exception as exc:
                from .diagnostics import failure

                if not getattr(exc, "voice_diagnosed", False):
                    failure(exc, engine=engine)
                worked = 0
                backoff = min(60, backoff * 2)
            if not worked:
                time.sleep(backoff)
    finally:
        engine.dispose()
