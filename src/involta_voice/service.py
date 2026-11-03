import hashlib
from typing import Any
from uuid import uuid4

from sqlalchemy import Connection, Engine, text

from .config import Settings
from .contracts import SubmissionMetadata
from .db import encoded, execute, row, snapshot
from .errors import VoiceError
from .storage import AudioStorage


def rate_limit(conn: Connection, key: str, limit: int, seconds: int) -> None:
    value = row(
        conn,
        """INSERT INTO intake_windows(key,count) VALUES(:key,1)
        ON CONFLICT(key) DO UPDATE SET
        count=CASE WHEN intake_windows.started_at<now()-make_interval(secs=>:seconds)
          THEN 1 ELSE intake_windows.count+1 END,
        started_at=CASE WHEN intake_windows.started_at<now()-make_interval(secs=>:seconds)
          THEN now() ELSE intake_windows.started_at END RETURNING count""",
        key=key,
        seconds=seconds,
    )
    if value and value["count"] > limit:
        raise VoiceError("Слишком много записей, попробуйте позже", "rate_limit", 429)


class VoiceService:
    def __init__(self, engine: Engine, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings
        self.storage = AudioStorage(settings)

    def submit(
        self, metadata: SubmissionMetadata, content: bytes, mime: str, purpose: str = "review"
    ) -> dict[str, Any]:
        if purpose not in ("review", "guide_transcription"):
            raise VoiceError("Неподдерживаемое назначение записи")
        sha = hashlib.sha256(content).hexdigest()
        payload_hash = hashlib.sha256(
            encoded(
                {
                    "purpose": purpose,
                    "sha256": sha,
                    "mime": mime,
                    "location": str(metadata.location.id),
                    "language": metadata.language,
                    "timezone": metadata.timezone,
                }
            ).encode()
        ).hexdigest()
        visitor, key = metadata.visitor, str(metadata.idempotency_key)
        with self.engine.connect() as conn:
            old = row(
                conn,
                "SELECT * FROM submissions WHERE visitor=:visitor AND idempotency_key=:key",
                visitor=visitor,
                key=key,
            )
        if old:
            self._same(old, payload_hash)
            return self.status(str(old["reference"]), visitor)
        name, duration = self.storage.save(content, mime)
        reference = str(uuid4())
        try:
            with self.engine.begin() as conn:
                execute(conn, "SELECT pg_advisory_xact_lock(47032002)")
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
                    rate_limit(conn, "visitor:" + visitor, 6, 900)
                    rate_limit(conn, "ip:" + metadata.ip_fingerprint, 30, 3600)
                    pending = row(
                        conn, "SELECT count(*) n FROM stages WHERE state IN ('queued','running')"
                    )
                    if pending and pending["n"] >= self.settings.queue_limit:
                        raise VoiceError("Очередь заполнена", "queue_full", 429)
                    signals = row(
                        conn,
                        """SELECT count(*) same_audio,
                        count(*) FILTER(WHERE visitor=:visitor) same_visitor
                        FROM (SELECT visitor,sha256 FROM submissions ORDER BY created_at DESC LIMIT 500) s
                        WHERE sha256=:sha""",
                        visitor=visitor,
                        sha=sha,
                    )
                    execute(
                        conn,
                        """INSERT INTO submissions(reference,location_id,visitor,idempotency_key,
                        payload_hash,metadata,audio,mime,duration,size,sha256,repeat_signals,purpose)
                        VALUES(:id,:location,:visitor,:key,:hash,CAST(:metadata AS jsonb),:audio,
                        :mime,:duration,:size,:sha,CAST(:signals AS jsonb),:purpose)""",
                        purpose=purpose,
                        id=reference,
                        location=str(metadata.location.id),
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
                    self._new_run(conn, reference, None)
            if old:
                self.storage.path(name).unlink(missing_ok=True)
        except VoiceError:
            self.storage.path(name).unlink(missing_ok=True)
            raise
        # On ambiguous database commit failure retain the file; orphan GC proves it unreferenced later.
        return self.status(reference, visitor)

    @staticmethod
    def _same(old: dict[str, Any], payload_hash: str) -> None:
        if old["payload_hash"] != payload_hash:
            raise VoiceError("Ключ уже использован для другой записи", "conflict", 409)

    def _new_run(
        self,
        conn: Connection,
        reference: str,
        transcript: str | None,
        request_key: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> int:
        previous = row(
            conn, "SELECT coalesce(max(number),0)+1 n FROM runs WHERE submission=:id", id=reference
        )
        number = int(previous["n"]) if previous else 1
        frozen = snapshot(conn, self.settings)
        submission = row(conn, "SELECT metadata FROM submissions WHERE reference=:id", id=reference)
        assert submission is not None
        frozen["location"] = context or submission["metadata"]["location"]
        execute(
            conn,
            "INSERT INTO runs(submission,number,snapshot,transcript,request_key) VALUES(:id,:n,CAST(:snapshot AS jsonb),:transcript,:key)",
            id=reference,
            n=number,
            snapshot=encoded(frozen),
            key=request_key,
            transcript=transcript,
        )
        execute(
            conn, "UPDATE submissions SET active_run=:n WHERE reference=:id", n=number, id=reference
        )
        execute(
            conn,
            "INSERT INTO stages(submission,run,kind) VALUES(:id,:n,:kind)",
            id=reference,
            n=number,
            kind="analysis" if transcript is not None else "transcription",
        )
        if transcript is None and frozen.get("asr_beta"):
            execute(
                conn,
                "INSERT INTO stages(submission,run,kind) VALUES(:id,:n,'comparison')",
                id=reference,
                n=number,
            )
        return number

    def new_run(
        self, reference: str, request_key: str | None = None, context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        with self.engine.begin() as conn:
            if row(conn, "SELECT 1 FROM archive_map WHERE submission=:id", id=reference):
                raise VoiceError(
                    "Архивная копия доступна только для чтения; исходная запись остаётся в Guide",
                    "conflict",
                    409,
                )
            submission = row(
                conn, "SELECT * FROM submissions WHERE reference=:id FOR UPDATE", id=reference
            )
            if not submission:
                raise VoiceError("Запись не найдена", "not_found", 404)
            if submission["purpose"] != "review":
                raise VoiceError(
                    "Внутренняя расшифровка Guide не создаёт обработки анализа", "conflict", 409
                )
            previous = (
                row(
                    conn,
                    "SELECT number FROM runs WHERE submission=:id AND request_key=:key",
                    id=reference,
                    key=request_key,
                )
                if request_key
                else None
            )
            if previous:
                return {"reference": reference, "run": previous["number"], "state": "accepted"}
            active = row(
                conn,
                "SELECT 1 FROM stages WHERE submission=:id AND (kind IN ('transcription','analysis') OR (:need_text AND kind='comparison')) AND state IN ('queued','running')",
                id=reference,
                need_text=submission["transcript"] is None,
            )
            if active:
                raise VoiceError("Обработка уже выполняется", "conflict", 409)
            if context and str(context["id"]) != str(submission["location_id"]):
                raise VoiceError("Нельзя изменить идентичность локации", "conflict", 409)
            number = self._new_run(conn, reference, submission["transcript"], request_key, context)
        return {"reference": reference, "run": number, "state": "accepted"}

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
            archived = row(
                conn, "SELECT source_review FROM archive_map WHERE submission=:id", id=reference
            )
            delivery = row(
                conn,
                "SELECT coalesce(max(sequence),0) n FROM outbox WHERE submission=:id AND run=:n",
                id=reference,
                n=submission["active_run"],
            )
            ack = row(conn, "SELECT sequence FROM delivery_ack WHERE consumer='guide'")
        current = [
            s
            for s in stages
            if s["run"] == submission["active_run"] and s["kind"] in ("transcription", "analysis")
        ]
        failed = next(
            (
                s
                for s in current
                if s["state"] in ("failed", "unknown")
                and (s["kind"] == "analysis" or submission["transcript"] is None)
            ),
            None,
        )
        fallback_pending = any(
            s["run"] == submission["active_run"]
            and s["kind"] == "comparison"
            and s["state"] in ("queued", "running")
            for s in stages
        )
        if failed and failed["kind"] == "transcription" and fallback_pending:
            failed = None
        if archived:
            state = "archived"
        elif failed:
            state = failed["kind"] + "_" + failed["state"]
        elif run and run["result"] is not None:
            state = (
                "delivered_to_guide"
                if delivery and delivery["n"] and ack and ack["sequence"] >= delivery["n"]
                else "result_ready"
            )
        elif submission["transcript"] is not None:
            state = "result_ready" if submission["purpose"] == "guide_transcription" else "analysis"
        elif any(s["state"] == "running" for s in current):
            state = "transcription"
        else:
            state = "accepted"
        result = {
            "reference": str(submission["reference"]),
            "location": submission["metadata"]["location"],
            "state": state,
            "run": submission["active_run"],
            "transcript": submission["transcript"],
            "created_at": str(submission["created_at"]),
            "error": failed["error"] if failed else None,
            "moderation": "pending",
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
                WHERE purpose='review' AND (CAST(:visitor AS text) IS NULL OR visitor=CAST(:visitor AS text))
                AND NOT EXISTS(SELECT 1 FROM archive_map a WHERE a.submission=submissions.reference)
                ORDER BY created_at DESC,reference LIMIT 30 OFFSET :offset"""),
                    {"visitor": visitor, "offset": (page - 1) * 30},
                )
                .scalars()
                .all()
            )
        return {"page": page, "rows": [self.status(str(reference), visitor) for reference in items]}

    def emit(
        self, conn: Connection, reference: str, number: int, kind: str, result: Any = None
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
            "schema_version": 1,
            "event_id": event_id,
            "kind": kind,
            "reference": reference,
            "run": number,
            "metadata": event_metadata,
            "transcript": submission["transcript"],
            "analysis": result,
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
