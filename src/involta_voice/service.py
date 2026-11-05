import hashlib
from typing import Any
from uuid import uuid4

from sqlalchemy import Connection, Engine, text

from .config import Settings
from .contracts import ReprocessRequest, SubmissionMetadata
from .db import encoded, execute, row, snapshot
from .errors import VoiceError
from .storage import AudioStorage


def rate_limit(
    conn: Connection, key: str, limit: int, seconds: int, location: int, epoch: int
) -> None:
    value = row(
        conn,
        """INSERT INTO intake_windows(key,count,location_id,content_epoch) VALUES(:key,1,:location,:epoch)
        ON CONFLICT(key) DO UPDATE SET
        count=CASE WHEN intake_windows.started_at<now()-make_interval(secs=>:seconds)
          THEN 1 ELSE intake_windows.count+1 END,
        started_at=CASE WHEN intake_windows.started_at<now()-make_interval(secs=>:seconds)
          THEN now() ELSE intake_windows.started_at END RETURNING count""",
        key=str(location) + ":" + str(epoch) + ":" + key,
        location=location,
        epoch=epoch,
        seconds=seconds,
    )
    if value and value["count"] > limit:
        raise VoiceError("Слишком много записей, попробуйте позже", "rate_limit", 429)


class VoiceService:
    def __init__(self, engine: Engine, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings
        self.storage = AudioStorage(settings)

    def submit(self, metadata: SubmissionMetadata, content: bytes, mime: str) -> dict[str, Any]:
        from .reset import lock_epoch

        sha = hashlib.sha256(content).hexdigest()
        location = metadata.location.id
        epoch = metadata.location.content_epoch
        payload_hash = hashlib.sha256(
            encoded(
                {
                    "sha256": sha,
                    "mime": mime,
                    "location": location,
                    "content_epoch": epoch,
                    "language": metadata.language,
                    "timezone": metadata.timezone,
                }
            ).encode()
        ).hexdigest()
        visitor, key = metadata.visitor, str(metadata.idempotency_key)
        reference = str(uuid4())
        name = None
        try:
            with self.engine.begin() as conn:
                if row(conn, "SELECT 1 FROM purge_files WHERE content_epoch=1 LIMIT 1"):
                    raise VoiceError(
                        "Переход Voice не завершён; требуется очистка файлов",
                        "transition_pending",
                        503,
                    )
                lock_epoch(conn, location, epoch)
                execute(
                    conn,
                    "SELECT pg_advisory_xact_lock(hashtextextended(:key,0))",
                    key=visitor + key,
                )
                old = row(
                    conn,
                    "SELECT * FROM submissions WHERE visitor=:visitor AND idempotency_key=:key",
                    visitor=visitor,
                    key=key,
                )
                if old:
                    self._same(old, payload_hash)
                    reference = str(old["reference"])
                else:
                    rate_limit(conn, "visitor:" + visitor, 6, 900, location, epoch)
                    rate_limit(conn, "ip:" + metadata.ip, 30, 3600, location, epoch)
                    pending = row(
                        conn, "SELECT count(*) n FROM stages WHERE state IN ('queued','running')"
                    )
                    if pending and pending["n"] >= self.settings.queue_limit:
                        raise VoiceError("Очередь заполнена", "queue_full", 429)
                    signals = row(
                        conn,
                        "SELECT count(*) same_audio,count(*) FILTER(WHERE visitor=:visitor) same_visitor FROM submissions WHERE sha256=:sha",
                        visitor=visitor,
                        sha=sha,
                    )
                    extension = {
                        "audio/webm": "webm",
                        "audio/ogg": "ogg",
                        "audio/mp4": "mp4",
                        "audio/wav": "wav",
                    }.get(mime, "invalid")
                    name = f"{location}.{epoch}.{reference}.{extension}"
                    name, duration = self.storage.save(
                        content, mime, name=name, min_duration_seconds=5
                    )
                    execute(
                        conn,
                        """INSERT INTO submissions(reference,location_id,content_epoch,visitor,idempotency_key,
                      payload_hash,metadata,audio,mime,duration,size,sha256,repeat_signals)
                      VALUES(:id,:location,:epoch,:visitor,:key,:hash,CAST(:metadata AS jsonb),:audio,:mime,:duration,:size,:sha,CAST(:signals AS jsonb))""",
                        id=reference,
                        location=location,
                        epoch=epoch,
                        visitor=visitor,
                        key=key,
                        hash=payload_hash,
                        metadata=metadata.model_dump_json(),
                        audio=name,
                        mime=mime,
                        duration=duration,
                        size=len(content),
                        sha=sha,
                        signals=encoded(signals or {}),
                    )
                    self._new_run(conn, reference)
                    self.emit(conn, reference, 1, "recording_received")
        except VoiceError:
            if name:
                self.storage.path(name).unlink(missing_ok=True)
            raise
        # An ambiguous commit retains an owner+epoch filename, discoverable by reset/GC.
        return self.status(reference, visitor)

    @staticmethod
    def _same(old: dict[str, Any], payload_hash: str) -> None:
        if old["payload_hash"] != payload_hash:
            raise VoiceError("Ключ уже использован для другой записи", "conflict", 409)

    def _new_run(
        self,
        conn: Connection,
        reference: str,
        number: int = 1,
        *,
        location_context: dict[str, Any] | None = None,
        reprocess_key: str | None = None,
    ) -> None:
        frozen = snapshot(conn, self.settings)
        submission = row(conn, "SELECT metadata FROM submissions WHERE reference=:id", id=reference)
        assert submission is not None
        frozen["location"] = location_context or submission["metadata"]["location"]
        if reprocess_key:
            frozen["reprocess_key"] = reprocess_key
        from .text import REVIEW_TEXT_FORMAT, catalogue_forms

        location = frozen["location"]
        frozen["name_forms"] = catalogue_forms(
            [location["name"], *location.get("recognition_dictionary", [])], city=location["name"]
        )
        from .variants import COMMON_RECOGNITION_NAMES

        frozen["common_recognition_names"] = list(COMMON_RECOGNITION_NAMES)
        frozen["name_forms"].update(
            {name.lower().replace("ё", "е"): name for name in COMMON_RECOGNITION_NAMES}
        )
        frozen["name_normalization"] = "catalogue-inflections-v1"
        frozen["review_text_format"] = REVIEW_TEXT_FORMAT
        if frozen["asr_beta"]:
            from .sherpa import options as sherpa_options
            from .variants import SHERPA_PROFILES, SHERPA_STAGES

            names = frozen["location"].get("recognition_dictionary", [])
            frozen["sherpa_models"] = {
                kind: sherpa_options(
                    self.settings, engine, variant, names, location_name=frozen["location"]["name"]
                )
                for kind, (engine, variant) in SHERPA_STAGES.items()
                if variant in SHERPA_PROFILES
            }
        execute(
            conn,
            "INSERT INTO runs(submission,number,snapshot) VALUES(:id,:number,CAST(:snapshot AS jsonb))",
            id=reference,
            number=number,
            snapshot=encoded(frozen),
        )
        if not frozen["asr_beta"]:
            execute(
                conn,
                "INSERT INTO stages(submission,run,kind) VALUES(:id,:number,'transcription')",
                id=reference,
                number=number,
            )
        if frozen["asr_beta"]:
            for kind in frozen["sherpa_models"]:
                execute(
                    conn,
                    "INSERT INTO stages(submission,run,kind) VALUES(:id,:number,:kind)",
                    id=reference,
                    number=number,
                    kind=kind,
                )

    def reprocess(
        self, reference: str, request: ReprocessRequest, actor_session: str | None = None
    ) -> dict[str, Any]:
        from .reset import lock_epoch

        location = request.location
        with self.engine.begin() as conn:
            lock_epoch(conn, location.id, location.content_epoch)
            submission = row(
                conn, "SELECT * FROM submissions WHERE reference=:id FOR UPDATE", id=reference
            )
            if not submission:
                raise VoiceError("Запись не найдена", "not_found", 404)
            if (
                submission["location_id"] != location.id
                or submission["content_epoch"] != location.content_epoch
            ):
                raise VoiceError("Контекст записи устарел", "stale_epoch", 409)
            previous = row(
                conn,
                "SELECT number FROM runs WHERE submission=:id AND snapshot->>'reprocess_key'=:key",
                id=reference,
                key=str(request.idempotency_key),
            )
            if previous:
                return {"reference": reference, "run": previous["number"], "state": "existing"}
            if row(
                conn,
                "SELECT 1 FROM stages WHERE submission=:id AND state IN ('queued','running')",
                id=reference,
            ):
                return {"reference": reference, "run": submission["active_run"], "state": "busy"}
            if not self.storage.path(submission["audio"]).is_file():
                raise VoiceError("Исходное аудио недоступно", "audio_missing", 409)
            pending = row(conn, "SELECT count(*) n FROM stages WHERE state IN ('queued','running')")
            if pending and pending["n"] >= self.settings.queue_limit:
                raise VoiceError("Очередь заполнена", "queue_full", 429)
            number = submission["active_run"] + 1
            self._new_run(
                conn,
                reference,
                number,
                location_context=location.model_dump(mode="json"),
                reprocess_key=str(request.idempotency_key),
            )
            execute(
                conn,
                "UPDATE submissions SET active_run=:number WHERE reference=:id",
                number=number,
                id=reference,
            )
            execute(
                conn,
                "INSERT INTO admin_events(id,action,session_id,before_data,after_data,location_id,content_epoch) VALUES(:id,'reprocess',:actor,CAST(:before AS jsonb),CAST(:after AS jsonb),:location,:epoch)",
                id=str(uuid4()),
                actor=actor_session,
                before=encoded({"reference": reference, "run": submission["active_run"]}),
                after=encoded({"reference": reference, "run": number}),
                location=location.id,
                epoch=location.content_epoch,
            )
            self.emit(conn, reference, number, "recording_received")
        return {"reference": reference, "run": number, "state": "queued"}

    def status(
        self, reference: str, visitor: str | None = None, *, admin: bool = False
    ) -> dict[str, Any]:
        with self.engine.connect() as conn:
            submission = row(conn, "SELECT * FROM submissions WHERE reference=:id", id=reference)
            if not submission or (visitor is not None and submission["visitor"] != visitor):
                raise VoiceError("Запись не найдена", "not_found", 404)
            stages = [
                dict(s)
                for s in conn.execute(
                    text("SELECT * FROM stages WHERE submission=:id ORDER BY run,kind"),
                    {"id": reference},
                ).mappings()
            ]
            run = row(
                conn,
                "SELECT * FROM runs WHERE submission=:id AND number=:n",
                id=reference,
                n=submission["active_run"],
            )
            from .variants import primary_kind, primary_variant

            frozen = run["snapshot"] if run else {}
            primary_text = row(
                conn,
                "SELECT transcript FROM asr_candidates WHERE submission=:id AND engine='gigaam' AND variant=:variant ORDER BY run DESC LIMIT 1",
                id=reference,
                variant=primary_variant(frozen),
            )
            # Historical fallback text is retained in storage, never exposed to visitors.
            submission["transcript"] = (
                submission["transcript"]
                if frozen.get("restore_dictionary_case") and primary_text
                else primary_text["transcript"]
                if primary_text
                else None
            )
            delivery = row(
                conn,
                "SELECT coalesce(max(sequence),0) n FROM outbox WHERE submission=:id AND run=:n",
                id=reference,
                n=submission["active_run"],
            )
            ack = row(conn, "SELECT sequence FROM delivery_ack WHERE consumer='guide'")
        frozen_primary = run["snapshot"]["transcription_model"] if run else "gigaam"
        engines = {
            "transcription": frozen_primary,
            "comparison": "parakeet" if frozen_primary == "gigaam" else "gigaam",
        }
        primary = next(
            (
                s
                for s in stages
                if s["run"] == submission["active_run"]
                and s["kind"] in engines
                and ((s["request"] or {}).get("model") or engines[s["kind"]]) == "gigaam"
            ),
            None,
        )
        if primary_variant(frozen) != "onnx":
            primary = next(
                (
                    s
                    for s in stages
                    if s["run"] == submission["active_run"] and s["kind"] == primary_kind(frozen)
                ),
                None,
            )
        failed = primary if primary and primary["state"] in ("failed", "unknown") else None
        if submission["transcript"] is not None:
            state = (
                "delivered_to_guide"
                if delivery and delivery["n"] and ack and ack["sequence"] >= delivery["n"]
                else "result_ready"
            )
        elif failed:
            state = "transcription_" + failed["state"]
        elif primary and primary["state"] == "running":
            state = "transcription"
        else:
            state = "accepted"
        location = dict(submission["metadata"]["location"])
        if not admin:
            location.pop("recognition_dictionary", None)
        result = {
            "reference": str(submission["reference"]),
            "location": location,
            "state": state,
            "run": submission["active_run"],
            "transcript": submission["transcript"],
            "created_at": str(submission["created_at"]),
            "error": failed["error"] if failed else None,
            "repeat_signals": submission["repeat_signals"],
        }
        if admin:
            result.update(
                metadata=submission["metadata"],
                stages=stages,
                result=run["result"] if run else None,
                sha256=submission["sha256"],
                mime=submission["mime"],
                duration=submission["duration"],
                size=submission["size"],
            )
        return result

    def listing(self, visitor: str | None = None, page: int = 1) -> dict[str, Any]:
        page = max(1, page)
        with self.engine.connect() as conn:
            items = (
                conn.execute(
                    text("""SELECT reference FROM submissions
                WHERE (CAST(:visitor AS text) IS NULL OR visitor=CAST(:visitor AS text))
                ORDER BY created_at DESC,reference LIMIT 30 OFFSET :offset"""),
                    {"visitor": visitor, "offset": (page - 1) * 30},
                )
                .scalars()
                .all()
            )
        return {"page": page, "rows": [self.status(str(reference), visitor) for reference in items]}

    def emit(
        self,
        conn: Connection,
        reference: str,
        number: int,
        kind: str,
        result: Any = None,
        *,
        error: str | None = None,
    ) -> None:
        # Lock BEFORE allocating the sequence: cursor order must also be commit order.
        execute(conn, "SELECT pg_advisory_xact_lock(47032003)")
        submission = row(conn, "SELECT * FROM submissions WHERE reference=:id", id=reference)
        assert submission is not None
        run = row(
            conn,
            "SELECT snapshot FROM runs WHERE submission=:id AND number=:n",
            id=reference,
            n=number,
        )
        event_metadata = {
            **submission["metadata"],
            "location": run["snapshot"]["location"] if run else submission["metadata"]["location"],
        }
        event_id = str(uuid4())
        payload = {
            "schema_version": 2,
            "event_id": event_id,
            "kind": kind,
            "reference": reference,
            "run": number,
            "metadata": event_metadata,
            "transcript": submission["transcript"] if kind == "gigaam_ready" else None,
            "analysis": result,
            "error": error,
            "sha256": submission["sha256"],
            "mime": submission["mime"],
            "duration": submission["duration"],
            "created_at": str(submission["created_at"]),
            "repeat_signals": submission["repeat_signals"],
        }
        execute(
            conn,
            """INSERT INTO outbox(event_id,submission,run,kind,payload)
            VALUES(:event,:id,:n,:kind,CAST(:payload AS jsonb))
            ON CONFLICT(submission,run,kind) DO NOTHING""",
            event=event_id,
            id=reference,
            n=number,
            kind=kind,
            payload=encoded(payload),
        )
