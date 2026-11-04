import time
from threading import Event
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy import text as sql_text

from involta_voice.contracts import OutboxEvent, SubmissionMetadata
from involta_voice.db import encoded, execute, row

from .client import VoiceClient


class Consumer:
    def __init__(self, engine: Engine, client: VoiceClient) -> None:
        self.engine, self.client = engine, client
        self.stopped = Event()

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
            if self.stopped.is_set():
                break
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
            if self.stopped.is_set():
                break
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
        import json

        if isinstance(exc, ValidationError):
            detail = json.dumps(
                [
                    {k: e[k] for k in ("loc", "type", "msg")}
                    for e in exc.errors(include_input=False, include_url=False)
                ],
                ensure_ascii=False,
            )
        elif isinstance(exc, ValueError):
            detail = str(exc)
        else:
            detail = "Не удалось применить событие; вход сохранён для повтора. Проверьте журнал consumer."
        with self.engine.begin() as conn:
            execute(
                conn,
                "UPDATE voice_inbox SET state='failed',error=:error WHERE event_id=:id AND state<>'imported'",
                error=(type(exc).__name__ + ": " + detail)[:4000],
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
        metadata = SubmissionMetadata.model_validate(event["metadata"])
        reference, event_id = str(UUID(event["reference"])), str(UUID(event["event_id"]))
        number = int(event["run"])
        transcript = event["transcript"]
        if event["kind"] == "gigaam_ready" and (not transcript or not transcript.strip()):
            raise ValueError("Invalid GigaAM transcript")
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
            # Old analysis/fallback events remain in the inbox as diagnostic history.
            # Only the explicit primary-result contract can publish a review.
            if event["kind"] not in ("recording_received", "gigaam_ready", "gigaam_failed"):
                execute(
                    conn,
                    "UPDATE voice_inbox SET state='imported',error=NULL WHERE event_id=:id",
                    id=event_id,
                )
                return 1
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
                    transcript,status,duration_seconds,created_at,result) VALUES(:id,:location,:epoch,:visitor,:audio,
                    :mime,:sha,:ip,:agent,NULL,'received',:duration,CAST(:created AS timestamptz),'{"entries":[],"suggestions":[]}'::jsonb)""",
                    id=reference,
                    location=str(metadata.location.id),
                    epoch=metadata.location.content_epoch,
                    visitor=metadata.visitor,
                    audio="voice:" + reference,
                    mime=event["mime"],
                    sha=event["sha256"],
                    ip=metadata.ip,
                    agent=metadata.user_agent,
                    created=event["created_at"],
                    duration=event["duration"],
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
            if event["kind"] == "gigaam_ready":
                execute(
                    conn,
                    "UPDATE reviews SET transcript=:text,status='transcribed',publication_status=1,error=NULL WHERE id=:id AND status<>'deleted'",
                    text=transcript,
                    id=reference,
                )
            elif event["kind"] == "gigaam_failed":
                execute(
                    conn,
                    """UPDATE reviews SET status='failed',publication_status=0,error=:error
                    WHERE id=:id AND status NOT IN ('transcribed','deleted')
                    AND :run >= (SELECT active_run FROM voice_links WHERE review_id=:id)""",
                    id=reference,
                    run=number,
                    error=event.get("error") or "GigaAM не распознал запись",
                )
            elif not row(
                conn,
                "SELECT 1 FROM voice_inbox WHERE review_id=:id AND kind='gigaam_ready' AND state='imported'",
                id=reference,
            ):
                # The receipt can arrive after the primary event; never erase a ready result.
                execute(
                    conn,
                    "UPDATE reviews SET status='received' WHERE id=:id AND status NOT IN ('transcribed','failed','deleted')",
                    id=reference,
                )
            execute(
                conn,
                "UPDATE voice_inbox SET state='imported',error=NULL WHERE event_id=:id",
                id=event_id,
            )
        return 1
