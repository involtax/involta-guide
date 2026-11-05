import json
import signal
from datetime import UTC, datetime
from threading import Event, Thread
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine, text

from .config import Settings
from .db import database, encoded, execute, row
from .errors import VoiceError
from .provider import FakeProvider, ProviderFailure, VoiceProvider
from .service import VoiceService
from .text import restore_names
from .variants import STAGES, primary_kind, primary_variant, result_identity


class Worker:
    def __init__(self, engine: Engine, settings: Settings, provider: Any = None) -> None:
        self.engine = engine
        self.settings = settings
        self.service = VoiceService(engine, settings)
        self.provider = provider or (
            FakeProvider() if settings.fake_provider else VoiceProvider(settings)
        )

    def claim(self) -> dict[str, Any] | None:
        with self.engine.begin() as conn:
            execute(conn, "SELECT pg_advisory_xact_lock(47032004)")
            # Retire outstanding Parakeet work without deleting its saved diagnostics.
            execute(
                conn,
                """UPDATE stages s SET state='failed',error='Parakeet удалён из эксперимента',
                finished_at=now(),lease_token=NULL,lease_until=NULL FROM runs r
                WHERE r.submission=s.submission AND r.number=s.run
                AND (s.state='queued' OR (s.state='running' AND s.lease_until<now()))
                AND (s.kind LIKE 'sherpa_parakeet_%' OR coalesce(s.request->>'model',
                  CASE s.kind WHEN 'transcription' THEN r.snapshot->>'transcription_model'
                    WHEN 'comparison' THEN CASE WHEN r.snapshot->>'transcription_model'='gigaam'
                    THEN 'parakeet' ELSE 'gigaam' END END)='parakeet')""",
            )
            # A saved response can be replayed. An interrupted external POST without one is unknown.
            expired = (
                conn.execute(
                    text(
                        """UPDATE stages SET state=CASE WHEN raw_response IS NOT NULL THEN 'queued' WHEN (request->>'engine' IN ('faster-whisper','onnx-asr','sherpa-onnx') OR kind LIKE 'sherpa_%') AND attempts<3 THEN 'queued' WHEN (request->>'engine' IN ('faster-whisper','onnx-asr','sherpa-onnx') OR kind LIKE 'sherpa_%') THEN 'failed' ELSE 'unknown' END,
                error=CASE WHEN raw_response IS NOT NULL THEN NULL WHEN (request->>'engine' IN ('faster-whisper','onnx-asr','sherpa-onnx') OR kind LIKE 'sherpa_%') THEN CASE WHEN attempts<3 THEN 'Локальный исполнитель прерван; ограниченный повтор' ELSE 'Локальный исполнитель прерван; исчерпаны попытки' END ELSE 'Исполнитель прерван; исход OpenAI неизвестен' END,
                lease_token=NULL,lease_until=NULL WHERE kind=ANY(:kinds) AND state='running' AND lease_until<now()
                RETURNING submission,run,kind,state,error"""
                    ),
                    {"kinds": list(STAGES)},
                )
                .mappings()
                .all()
            )
            for interrupted in expired:
                frozen = row(
                    conn,
                    "SELECT snapshot FROM runs WHERE submission=:id AND number=:n",
                    id=interrupted["submission"],
                    n=interrupted["run"],
                )
                if (
                    frozen
                    and interrupted["kind"] == primary_kind(frozen["snapshot"])
                    and interrupted["state"]
                    in (
                        "failed",
                        "unknown",
                    )
                ):
                    self.service.emit(
                        conn,
                        str(interrupted["submission"]),
                        interrupted["run"],
                        "gigaam_failed",
                        error=self.clean(interrupted["error"]),
                    )
            running = row(conn, "SELECT count(*) n FROM stages WHERE state='running'")
            if running and running["n"] >= self.settings.concurrency:
                return None
            local_running = row(
                conn, "SELECT 1 FROM stages WHERE kind<>'analysis' AND state='running'"
            )
            stage = row(
                conn,
                """SELECT s.* FROM stages s JOIN runs r ON r.submission=s.submission AND r.number=s.run
                WHERE s.kind=ANY(:kinds) AND s.state='queued' AND s.available_at<=now() AND :local_busy=false
                ORDER BY CASE WHEN s.kind=CASE WHEN coalesce(r.snapshot->>'primary_variant','onnx')='onnx'
                  THEN 'transcription' ELSE 'sherpa_gigaam_'||substr(r.snapshot->>'primary_variant',8) END
                  THEN 0 WHEN s.kind='transcription' THEN 1 ELSE 2 END,
                s.available_at,s.submission,s.run,s.kind FOR UPDATE OF s SKIP LOCKED LIMIT 1""",
                kinds=list(STAGES),
                local_busy=bool(local_running),
            )
            if not stage:
                return None
            token = str(uuid4())
            execute(
                conn,
                """UPDATE stages SET state='running',attempts=attempts+1,lease_token=:token,
                lease_until=now()+make_interval(secs=>:lease),started_at=now(),error=NULL
                WHERE submission=:id AND run=:n AND kind=:kind""",
                token=token,
                lease=self.settings.lease_seconds,
                id=stage["submission"],
                n=stage["run"],
                kind=stage["kind"],
            )
            stage["lease_token"] = token
            stage["attempts"] += 1
            return stage

    def once(self) -> bool:
        stage = self.claim()
        if stage is None:
            return False
        try:
            self.process(stage)
        except ProviderFailure as exc:
            state = (
                "unknown"
                if exc.unknown
                else "queued"
                if exc.retry and stage["attempts"] < 3
                else "failed"
            )
            self.fail(stage, str(exc), state)
        except (ValueError, KeyError, TypeError) as exc:
            # Both engines keep their own result or diagnostic failure.
            self.fail(stage, "Ответ не прошёл проверку: " + self.clean(str(exc))[:2000])
        return True

    def clean(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: self.clean(v)
                for k, v in value.items()
                if k.lower() not in ("authorization", "cookie", "set-cookie", "api_key")
            }
        if isinstance(value, list):
            return [self.clean(v) for v in value]
        if isinstance(value, str):
            for secret in (
                self.settings.openai_api_key,
                self.settings.service_secret,
                self.settings.database_url,
            ):
                if secret.get_secret_value():
                    value = value.replace(secret.get_secret_value(), "[REDACTED]")
        return value

    def fenced(self, conn: Any, stage: dict[str, Any]) -> None:
        submission = row(
            conn,
            "SELECT location_id,content_epoch FROM submissions WHERE reference=:id",
            id=stage["submission"],
        )
        if not submission:
            raise ProviderFailure("Worker lease lost", unknown=True)
        from .reset import lock_epoch

        try:
            lock_epoch(conn, submission["location_id"], submission["content_epoch"])
        except VoiceError:
            raise ProviderFailure("Content epoch changed", unknown=True) from None
        valid = row(
            conn,
            """SELECT 1 FROM stages WHERE submission=:id AND run=:n AND kind=:kind
            AND state='running' AND lease_token=:token AND lease_until>now() FOR UPDATE""",
            id=stage["submission"],
            n=stage["run"],
            kind=stage["kind"],
            token=stage["lease_token"],
        )
        if not valid:
            raise ProviderFailure("Worker lease lost", unknown=True)

    def process(self, stage: dict[str, Any]) -> None:
        reference, number, kind = str(stage["submission"]), stage["run"], stage["kind"]
        with self.engine.connect() as conn:
            submission = row(conn, "SELECT * FROM submissions WHERE reference=:id", id=reference)
            run = row(
                conn,
                "SELECT * FROM runs WHERE submission=:id AND number=:n",
                id=reference,
                n=number,
            )
        if not submission or not run:
            return
        snapshot = run["snapshot"]
        primary = snapshot["transcription_model"]
        engine, variant = result_identity(kind, primary)
        # A saved response belongs to its frozen request, including pre-transition jobs.
        request = stage.get("request") or (
            snapshot["asr_models"][engine] if variant == "onnx" else snapshot["sherpa_models"][kind]
        )
        engine = request["model"]
        raw = stage.get("raw_response")
        if raw is None:
            call_id = str(uuid4())
            with self.engine.begin() as conn:
                self.fenced(conn, stage)
                execute(
                    conn,
                    "INSERT INTO provider_calls(id,submission,run,kind,request) VALUES(:call,:id,:n,:kind,CAST(:request AS jsonb))",
                    call=call_id,
                    id=reference,
                    n=number,
                    kind=kind,
                    request=encoded(self.clean(request)),
                )
                execute(
                    conn,
                    "UPDATE stages SET request=CAST(:request AS jsonb) WHERE submission=:id AND run=:n AND kind=:kind",
                    id=reference,
                    n=number,
                    kind=kind,
                    request=encoded(self.clean(request)),
                )
            try:
                audio = self.service.storage.path(submission["audio"]).read_bytes()
                stop_heartbeat = Event()

                def renew() -> None:
                    while not stop_heartbeat.wait(min(10, self.settings.lease_seconds / 3)):
                        try:
                            with self.engine.begin() as conn:
                                execute(
                                    conn,
                                    "UPDATE stages SET lease_until=now()+make_interval(secs=>:lease) WHERE submission=:id AND run=:run AND kind=:kind AND state='running' AND lease_token=:token",
                                    kind=kind,
                                    lease=self.settings.lease_seconds,
                                    id=reference,
                                    run=number,
                                    token=stage["lease_token"],
                                )
                            self.settings.heartbeat_file.write_text(datetime.now(UTC).isoformat())
                        except Exception:
                            return

                heartbeat = Thread(target=renew, daemon=True)
                heartbeat.start()
                try:
                    raw = self.provider.call(
                        "transcription",
                        request,
                        audio,
                        submission["mime"],
                    )
                finally:
                    stop_heartbeat.set()
                    heartbeat.join(timeout=2)
            except (ProviderFailure, OSError) as exc:
                with self.engine.begin() as conn:
                    execute(
                        conn,
                        "UPDATE provider_calls SET error=:error WHERE id=:call",
                        error=self.clean(str(exc)),
                        call=call_id,
                    )
                if isinstance(exc, ProviderFailure):
                    raise
                raise ProviderFailure("Исходная аудиозапись недоступна") from exc
            raw = self.clean(raw)
            # Commit raw provider output BEFORE parsing it, independently of validation.
            with self.engine.begin() as conn:
                usage: dict[str, Any] = {}
                try:
                    usage = json.loads(raw["body"]).get("usage") or {}
                except (ValueError, AttributeError):
                    pass
                execute(
                    conn,
                    "UPDATE provider_calls SET response=CAST(:response AS jsonb),input_tokens=:input,output_tokens=:output WHERE id=:call",
                    response=encoded(raw),
                    call=call_id,
                    input=usage.get("input_tokens"),
                    output=usage.get("output_tokens"),
                )
                execute(
                    conn,
                    """UPDATE stages SET raw_response=CAST(:raw AS jsonb)
                    WHERE submission=:id AND run=:n AND kind=:kind AND
                    (lease_token=:token OR (state='unknown' AND lease_token IS NULL AND attempts=:attempt))""",
                    raw=encoded(raw),
                    id=reference,
                    n=number,
                    kind=kind,
                    token=stage["lease_token"],
                    attempt=stage["attempts"],
                )
        status = raw["status_code"]
        if status >= 400:
            raise ProviderFailure(
                f"ASR HTTP {status}; request_id={raw.get('request_id') or 'unknown'}",
                unknown=status >= 500 or status == 408,
                retry=status == 429,
            )
        body = json.loads(raw["body"])
        if not isinstance(body, dict):
            raise ValueError("ASR должен вернуть JSON-объект")
        transcript = body.get("text")
        if not isinstance(transcript, str) or not transcript.strip() or len(transcript) > 30000:
            raise ValueError("Пустая или слишком длинная расшифровка")
        from .recognition import save_candidate

        with self.engine.begin() as conn:
            self.fenced(conn, stage)
            save_candidate(
                conn,
                reference,
                number,
                request,
                {
                    **body,
                    "is_fake": bool(
                        self.settings.fake_provider
                        or str(raw.get("request_id") or "").startswith("fake")
                    ),
                },
                submission,
            )
            self.complete(conn, stage)
            if engine != "gigaam" or request.get("variant", "onnx") != primary_variant(snapshot):
                return
            if snapshot.get("restore_dictionary_case"):
                location = snapshot["location"]
                transcript = restore_names(
                    transcript,
                    [location["name"], *location.get("recognition_dictionary", [])],
                    forms=snapshot.get("name_forms"),
                )
            execute(
                conn,
                "UPDATE submissions SET transcript=:text WHERE reference=:id",
                id=reference,
                text=transcript,
            )
            execute(
                conn,
                "UPDATE runs SET transcript=:text,transcription_engine='gigaam' WHERE submission=:id AND number=:n",
                id=reference,
                n=number,
                text=transcript,
            )
            self.service.emit(conn, reference, number, "gigaam_ready")

    @staticmethod
    def complete(conn: Any, stage: dict[str, Any]) -> None:
        execute(
            conn,
            """UPDATE stages SET state='completed',finished_at=now(),lease_token=NULL,lease_until=NULL,error=NULL
            WHERE submission=:id AND run=:n AND kind=:kind""",
            id=stage["submission"],
            n=stage["run"],
            kind=stage["kind"],
        )

    def fail(self, stage: dict[str, Any], error: str, state: str = "failed") -> None:
        with self.engine.begin() as conn:
            changed = row(
                conn,
                """UPDATE stages SET state=:state,error=:error,finished_at=now(),lease_token=NULL,
                lease_until=NULL,available_at=now()+interval '30 seconds',
                raw_response=CASE WHEN :state='queued' THEN NULL ELSE raw_response END
                WHERE submission=:id AND run=:n AND kind=:kind AND lease_token=:token RETURNING submission""",
                state=state,
                error=error,
                id=stage["submission"],
                n=stage["run"],
                kind=stage["kind"],
                token=stage["lease_token"],
            )
            frozen = row(
                conn,
                "SELECT snapshot FROM runs WHERE submission=:id AND number=:n",
                id=stage["submission"],
                n=stage["run"],
            )
            if (
                changed
                and frozen
                and stage["kind"] == primary_kind(frozen["snapshot"])
                and state in ("failed", "unknown")
            ):
                self.service.emit(
                    conn,
                    str(stage["submission"]),
                    stage["run"],
                    "gigaam_failed",
                    error=self.clean(error),
                )


def main() -> None:
    settings = Settings()
    engine = database(settings)
    worker = Worker(engine, settings)
    if isinstance(worker.provider, VoiceProvider) and not settings.asr_beta_enabled:
        worker.provider.asr.warmup()
    stopped = Event()

    def stop(signum: int, frame: Any) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopped.is_set():
            settings.heartbeat_file.write_text(datetime.now(UTC).isoformat())
            try:
                worked = worker.once()
            except Exception:
                # The durable lease recovers after process/database failure; do not leak secrets.
                worked = False
            if not worked:
                stopped.wait(1)
    finally:
        worker.provider.close()
        engine.dispose()
