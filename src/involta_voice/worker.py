import json
import signal
from datetime import UTC, datetime
from threading import Event, Thread
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine

from .config import Settings
from .contracts import AnalysisResult, LocationContext, validate_analysis
from .db import database, encoded, execute, row
from .errors import VoiceError
from .provider import FakeProvider, ProviderFailure, VoiceProvider
from .service import VoiceService


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
            # A saved response can be replayed. An interrupted external POST without one is unknown.
            execute(
                conn,
                """UPDATE stages SET state=CASE WHEN raw_response IS NOT NULL THEN 'queued' WHEN request->>'engine' IN ('faster-whisper','onnx-asr') AND attempts<3 THEN 'queued' WHEN request->>'engine' IN ('faster-whisper','onnx-asr') THEN 'failed' ELSE 'unknown' END,
                error=CASE WHEN raw_response IS NOT NULL THEN NULL WHEN request->>'engine' IN ('faster-whisper','onnx-asr') THEN 'Локальный исполнитель прерван; ограниченный повтор' ELSE 'Исполнитель прерван; исход OpenAI неизвестен' END,
                lease_token=NULL,lease_until=NULL WHERE state='running' AND lease_until<now()""",
            )
            running = row(conn, "SELECT count(*) n FROM stages WHERE state='running'")
            if running and running["n"] >= self.settings.concurrency:
                return None
            local_running = row(
                conn, "SELECT 1 FROM stages WHERE kind<>'analysis' AND state='running'"
            )
            stage = row(
                conn,
                """SELECT s.* FROM stages s WHERE state='queued' AND available_at<=now() AND (:local_busy=false OR kind='analysis')
                ORDER BY CASE kind WHEN 'transcription' THEN 0 WHEN 'analysis' THEN 1 ELSE 2 END,available_at,submission,run,kind FOR UPDATE SKIP LOCKED LIMIT 1""",
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
            # Details are private and sanitized; the transcript is already independent of analysis.
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
        assert submission and run
        context = LocationContext.model_validate(run["snapshot"]["location"])
        snapshot = run["snapshot"]
        if kind != "analysis":
            request = dict(
                snapshot.get("transcription") or {"model": snapshot["transcription_model"]}
            )
            if kind in ("comparison", "evaluate_gigaam", "evaluate_parakeet"):
                engine = (
                    ("parakeet" if snapshot["transcription_model"] == "gigaam" else "gigaam")
                    if kind == "comparison"
                    else kind.removeprefix("evaluate_")
                )
                request = snapshot["asr_models"][engine]

        else:
            request = {
                "model": snapshot["analysis_model"],
                "max_output_tokens": snapshot["max_output_tokens"],
                "input": [
                    {"role": "system", "content": snapshot["prompt"]},
                    {
                        "role": "user",
                        "content": encoded(
                            {
                                "transcript": run["transcript"],
                                "context": context.model_dump(mode="json"),
                            }
                        ),
                    },
                ],
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "voice_analysis_v1",
                        "schema": AnalysisResult.model_json_schema(),
                        "strict": False,
                    }
                },
            }
        raw = stage.get("raw_response")
        if raw is None:
            call_id = str(uuid4())
            with self.engine.begin() as conn:
                self.fenced(conn, stage)
                execute(conn, "SELECT pg_advisory_xact_lock(47032005)")
                count = row(
                    conn,
                    "SELECT count(*) n FROM provider_calls WHERE kind='analysis' AND created_at>=date_trunc('day',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'",
                )
                if kind == "analysis" and count and count["n"] >= self.settings.daily_calls:
                    raise ProviderFailure("Дневной лимит обращений Voice к OpenAI исчерпан")
                reserved = row(
                    conn,
                    "SELECT coalesce(sum(coalesce(cost_usd,reserved_usd)),0) usd FROM provider_calls WHERE kind='analysis' AND created_at>=date_trunc('day',now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'",
                )
                if (
                    kind == "analysis"
                    and reserved
                    and reserved["usd"] + self.settings.per_call_reserve_usd
                    > self.settings.daily_budget_usd
                ):
                    raise ProviderFailure("Дневной бюджет Voice исчерпан")
                execute(
                    conn,
                    "INSERT INTO provider_calls(id,submission,run,kind,request,reserved_usd) VALUES(:call,:id,:n,:kind,CAST(:request AS jsonb),:reserve)",
                    call=call_id,
                    id=reference,
                    n=number,
                    kind=kind,
                    request=encoded(self.clean(request)),
                    reserve=self.settings.per_call_reserve_usd if kind == "analysis" else 0,
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
                audio = (
                    self.service.storage.path(submission["audio"]).read_bytes()
                    if kind != "analysis"
                    else b""
                )
                if kind != "analysis":
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
                                self.settings.heartbeat_file.write_text(
                                    datetime.now(UTC).isoformat()
                                )
                            except Exception:
                                return

                    heartbeat = Thread(target=renew, daemon=True)
                    heartbeat.start()
                    try:
                        raw = self.provider.call(
                            "transcription" if kind != "analysis" else "analysis",
                            request,
                            audio,
                            submission["mime"],
                        )
                    finally:
                        stop_heartbeat.set()
                        heartbeat.join(timeout=2)
                else:
                    raw = self.provider.call(
                        "transcription" if kind != "analysis" else "analysis",
                        request,
                        audio,
                        submission["mime"],
                    )
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
                prices = snapshot["prices"].get(request["model"], {})
                cost = 0.0 if kind != "analysis" else None
                if (
                    "input" in prices
                    and "output" in prices
                    and "input_tokens" in usage
                    and "output_tokens" in usage
                ):
                    cost = (
                        usage["input_tokens"] * prices["input"]
                        + usage["output_tokens"] * prices["output"]
                    ) / 1_000_000
                execute(
                    conn,
                    "UPDATE provider_calls SET response=CAST(:response AS jsonb),input_tokens=:input,output_tokens=:output,cost_usd=:cost WHERE id=:call",
                    response=encoded(raw),
                    call=call_id,
                    input=usage.get("input_tokens"),
                    output=usage.get("output_tokens"),
                    cost=cost,
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
                f"OpenAI HTTP {status}; request_id={raw.get('request_id') or 'unknown'}",
                unknown=status >= 500 or status == 408,
                retry=status == 429,
            )
        body = json.loads(raw["body"])
        if not isinstance(body, dict):
            raise ValueError("OpenAI должен вернуть JSON-объект")
        if kind != "analysis":
            transcript = body.get("text")
            if not isinstance(transcript, str) or not transcript.strip() or len(transcript) > 30000:
                raise ValueError("Пустая или слишком длинная расшифровка")
            from .evaluation import save_candidate

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
                evaluation_only = bool(snapshot.get("asr_evaluation_only"))
                primary_failed = row(
                    conn,
                    "SELECT 1 FROM stages WHERE submission=:id AND run=:n AND kind='transcription' AND state IN ('failed','unknown')",
                    id=reference,
                    n=number,
                )
                if evaluation_only or (kind != "transcription" and not primary_failed):
                    return
                promoted = row(
                    conn,
                    "UPDATE submissions SET transcript=:text WHERE reference=:id AND transcript IS NULL RETURNING reference",
                    id=reference,
                    text=transcript,
                )
                if not promoted:
                    return
                execute(
                    conn,
                    "UPDATE runs SET transcript=:text,transcription_engine=:engine WHERE submission=:id AND number=:n AND transcript IS NULL",
                    id=reference,
                    n=number,
                    text=transcript,
                    engine=request["model"],
                )
                if submission["purpose"] == "guide_transcription":
                    return
                self.service.emit(conn, reference, number, "transcript_ready")
                execute(
                    conn,
                    "INSERT INTO stages(submission,run,kind) VALUES(:id,:n,'analysis') ON CONFLICT DO NOTHING",
                    id=reference,
                    n=number,
                )

        else:
            result = self.parse_analysis(body, run["transcript"], context)
            with self.engine.begin() as conn:
                self.fenced(conn, stage)
                execute(
                    conn,
                    "UPDATE runs SET result=CAST(:result AS jsonb) WHERE submission=:id AND number=:n",
                    result=result.model_dump_json(),
                    id=reference,
                    n=number,
                )
                self.complete(conn, stage)
                self.service.emit(
                    conn, reference, number, "analysis_ready", result.model_dump(mode="json")
                )

    @staticmethod
    def parse_analysis(body: Any, transcript: str, context: LocationContext) -> AnalysisResult:
        if not isinstance(body, dict):
            raise ValueError("Expected provider object")
        if body.get("status") != "completed":
            raise ValueError("Незавершённый ответ анализа; исходный ответ сохранён")
        output = "".join(
            c.get("text", "")
            for item in body.get("output", [])
            for c in item.get("content", [])
            if c.get("type") == "output_text"
        )
        return validate_analysis(json.loads(output), transcript, context)

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
            updated = row(
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
            if updated and state in ("failed", "unknown"):
                submission = row(
                    conn,
                    "SELECT transcript FROM submissions WHERE reference=:id",
                    id=stage["submission"],
                )
                if submission and submission["transcript"] is not None:
                    self.service.emit(
                        conn,
                        str(stage["submission"]),
                        stage["run"],
                        "processing_failed",
                        {"stage": stage["kind"], "state": state, "error": error},
                    )

    def revalidate(self, reference: str, number: int) -> None:
        with self.engine.begin() as conn:
            run = row(
                conn,
                "SELECT * FROM runs WHERE submission=:id AND number=:n FOR UPDATE",
                id=reference,
                n=number,
            )
            stage = row(
                conn,
                "SELECT * FROM stages WHERE submission=:id AND run=:n AND kind='analysis' FOR UPDATE",
                id=reference,
                n=number,
            )
            if stage is None:
                stage = row(
                    conn,
                    "SELECT * FROM stages WHERE submission=:id AND run=:n AND kind='transcription' FOR UPDATE",
                    id=reference,
                    n=number,
                )
            submission = row(conn, "SELECT * FROM submissions WHERE reference=:id", id=reference)
            if not run or not stage or not submission or stage["raw_response"] is None:
                raise VoiceError("Нет сохранённого ответа для проверки", "conflict", 409)
            if stage["state"] in ("running", "queued"):
                raise VoiceError("Обработка выполняется", "conflict", 409)
            if run["result"] is not None:
                return
            if stage["kind"] == "transcription":
                raw = stage["raw_response"]
                transcript = (
                    json.loads(raw["body"]).get("text") if raw.get("status_code") == 200 else None
                )
                if (
                    not isinstance(transcript, str)
                    or not transcript.strip()
                    or len(transcript) > 30000
                ):
                    raise VoiceError("Сохранённая расшифровка не прошла проверку")
                if submission["transcript"] is not None and submission["transcript"] != transcript:
                    raise VoiceError(
                        "Исходный текст уже сохранён другой обработкой", "conflict", 409
                    )
                execute(
                    conn,
                    "UPDATE submissions SET transcript=coalesce(transcript,:text) WHERE reference=:id",
                    id=reference,
                    text=transcript,
                )
                execute(
                    conn,
                    "UPDATE runs SET transcript=:text WHERE submission=:id AND number=:n",
                    id=reference,
                    n=number,
                    text=transcript,
                )
                self.complete(conn, stage)
                if submission["purpose"] == "guide_transcription":
                    return
                self.service.emit(conn, reference, number, "transcript_ready")
                execute(
                    conn,
                    "INSERT INTO stages(submission,run,kind) VALUES(:id,:n,'analysis') ON CONFLICT DO NOTHING",
                    id=reference,
                    n=number,
                )
                return
            result = self.parse_analysis(
                json.loads(stage["raw_response"]["body"]),
                run["transcript"],
                LocationContext.model_validate(run["snapshot"]["location"]),
            )
            execute(
                conn,
                "UPDATE runs SET result=CAST(:result AS jsonb) WHERE submission=:id AND number=:n",
                result=result.model_dump_json(),
                id=reference,
                n=number,
            )
            self.complete(conn, stage)
            self.service.emit(
                conn, reference, number, "analysis_ready", result.model_dump(mode="json")
            )


def main() -> None:
    settings = Settings()
    engine = database(settings)
    worker = Worker(engine, settings)
    if isinstance(worker.provider, VoiceProvider):
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
