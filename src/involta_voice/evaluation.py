"""ASR-only comparison runs and human beta annotations; no Guide mutations/outbox."""

import hashlib
import json
import re
import unicodedata
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, text

from .asr import MODELS
from .db import encoded, execute, row, snapshot
from .errors import VoiceError


def save_candidate(
    conn: Any,
    reference: str,
    run: int,
    request: dict[str, Any],
    body: dict[str, Any],
    submission: dict[str, Any],
) -> None:
    engine = request["model"]
    if engine not in MODELS:
        return  # Historical saved response can still be revalidated.
    details = {**body, "audio_sha256": submission["sha256"]}
    execute(
        conn,
        """INSERT INTO asr_candidates(submission,run,engine,revision,settings,transcript,elapsed_seconds,duration,details)
    VALUES(:id,:run,:engine,:revision,CAST(:settings AS jsonb),:text,:elapsed,:duration,CAST(:details AS jsonb))
    ON CONFLICT(submission,run,engine) DO NOTHING""",
        id=reference,
        run=run,
        engine=engine,
        revision=request["revision"],
        settings=encoded(request),
        text=body["text"],
        elapsed=body.get("elapsed_seconds", 0),
        duration=submission["duration"],
        details=encoded(details),
    )


def queue_evaluation(
    engine: Engine, settings: Any, reference: str, key: str, force: bool = False
) -> dict[str, Any]:
    with engine.begin() as conn:
        submission = row(
            conn, "SELECT * FROM submissions WHERE reference=:id FOR UPDATE", id=reference
        )
        if not submission:
            raise VoiceError("Запись не найдена", "not_found", 404)
        old = row(
            conn,
            "SELECT * FROM runs WHERE submission=:id AND request_key=:key",
            id=reference,
            key=key,
        )
        if old:
            if not old["snapshot"].get("asr_evaluation_only"):
                raise VoiceError("Ключ уже использован", "conflict", 409)
            return {"reference": reference, "run": old["number"]}
        frozen = snapshot(conn, settings)
        frozen.update(asr_evaluation_only=True, location=submission["metadata"]["location"])
        ordinal = row(
            conn, "SELECT coalesce(max(number),0)+1 n FROM runs WHERE submission=:id", id=reference
        )
        assert ordinal is not None
        if not force:
            existing = (
                conn.execute(
                    text("SELECT * FROM runs WHERE submission=:id ORDER BY number DESC"),
                    {"id": reference},
                )
                .mappings()
                .all()
            )
            for previous in existing:
                kinds = {
                    r["kind"]
                    for r in conn.execute(
                        text(
                            "SELECT kind FROM stages WHERE submission=:id AND run=:n AND kind<>'analysis'"
                        ),
                        {"id": reference, "n": previous["number"]},
                    ).mappings()
                }
                if previous["snapshot"].get("asr_models") == frozen["asr_models"] and (
                    {"transcription", "comparison"} <= kinds
                    or {"evaluate_gigaam", "evaluate_parakeet"} <= kinds
                ):
                    return {"reference": reference, "run": previous["number"], "reused": True}
        number = ordinal["n"]
        execute(
            conn,
            "INSERT INTO runs(submission,number,snapshot,request_key) VALUES(:id,:n,CAST(:snapshot AS jsonb),:key)",
            id=reference,
            n=number,
            snapshot=encoded(frozen),
            key=key,
        )
        for name in MODELS:
            execute(
                conn,
                "INSERT INTO stages(submission,run,kind) VALUES(:id,:n,:kind)",
                id=reference,
                n=number,
                kind="evaluate_" + name,
            )
        # active_run / transcript / moderation / published data remain untouched.
        return {"reference": reference, "run": number}


def normalize(value: str) -> str:
    return " ".join(
        re.findall(r"\w+", unicodedata.normalize("NFKC", value).casefold().replace("ё", "е"))
    )


def distance(a: Any, b: Any) -> int:
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (x != y)))
        previous = current
    return previous[-1]


def metrics(hypothesis: str, reference: str | None) -> dict[str, Any]:
    if not reference:
        return {"wer": None, "cer": None}
    truth = normalize(reference)
    predicted = normalize(hypothesis)
    return {
        "wer": distance(truth.split(), predicted.split()) / len(truth.split()) if truth else None,
        "cer": distance(truth.replace(" ", ""), predicted.replace(" ", ""))
        / len(truth.replace(" ", ""))
        if truth
        else None,
    }


def report(engine: Engine, reference: str) -> dict[str, Any]:
    with engine.connect() as conn:
        if not row(conn, "SELECT 1 FROM submissions WHERE reference=:id", id=reference):
            raise VoiceError("Запись не найдена", "not_found", 404)
        runs = []
        numbers = (
            conn.execute(
                text(
                    "SELECT DISTINCT run FROM stages WHERE submission=:id AND kind<>'analysis' ORDER BY run DESC"
                ),
                {"id": reference},
            )
            .scalars()
            .all()
        )
        for number in numbers:
            candidates = [
                dict(r)
                for r in conn.execute(
                    text(
                        "SELECT * FROM asr_candidates WHERE submission=:id AND run=:n ORDER BY engine"
                    ),
                    {"id": reference, "n": number},
                ).mappings()
            ]
            states = [
                dict(r)
                for r in conn.execute(
                    text(
                        "SELECT kind,state,error,request FROM stages WHERE submission=:id AND run=:n AND kind<>'analysis' ORDER BY kind"
                    ),
                    {"id": reference, "n": number},
                ).mappings()
            ]
            stored_run = row(
                conn,
                "SELECT snapshot,transcription_engine FROM runs WHERE submission=:id AND number=:n",
                id=reference,
                n=number,
            )
            assert stored_run is not None
            frozen = stored_run["snapshot"]
            evaluation = row(
                conn,
                "SELECT * FROM asr_evaluations WHERE submission=:id AND run=:n",
                id=reference,
                n=number,
            )
            digest = hashlib.sha256(
                encoded({"candidates": candidates, "states": states}).encode()
            ).hexdigest()
            presented = []
            for name in MODELS:
                candidate = next((c for c in candidates if c["engine"] == name), None)
                stage = next(
                    (
                        s
                        for s in states
                        if (s["request"] or {}).get("model") == name
                        or s["kind"] == "evaluate_" + name
                        or (
                            s["kind"] == "transcription"
                            and frozen.get("transcription_model") == name
                        )
                        or (s["kind"] == "comparison" and frozen.get("transcription_model") != name)
                    ),
                    None,
                )
                presented.append(
                    {
                        "engine": name,
                        "name": MODELS[name]["name"],
                        "state": stage["state"] if stage else "not_scheduled",
                        "error": stage["error"] if stage else None,
                        "text": candidate["transcript"] if candidate else None,
                        "revision": candidate["revision"]
                        if candidate
                        else frozen.get("asr_models", {}).get(name, {}).get("revision"),
                        "seconds": candidate["elapsed_seconds"] if candidate else None,
                        "audio_sha256": candidate["details"].get("audio_sha256")
                        if candidate
                        else None,
                        "rtf": candidate["elapsed_seconds"] / candidate["duration"]
                        if candidate and candidate["duration"]
                        else None,
                        **(
                            metrics(
                                candidate["transcript"],
                                evaluation["reference_text"] if evaluation else None,
                            )
                            if candidate
                            else {"wer": None, "cer": None}
                        ),
                    }
                )
            runs.append(
                {
                    "run": number,
                    "candidates": presented,
                    "digest": digest,
                    "evaluation": evaluation,
                    "primary_engine": frozen.get("transcription_model"),
                    "source_engine": stored_run["transcription_engine"],
                    "evaluation_only": bool(frozen.get("asr_evaluation_only")),
                }
            )
    return json.loads(encoded({"reference": reference, "runs": runs}))


class Evaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: UUID
    revision: int = Field(ge=0)
    candidates_digest: str = Field(min_length=64, max_length=64)
    preferred_engine: str | None = None
    reference_text: str | None = Field(default=None, max_length=30000)
    note: str = Field(default="", max_length=2000)


def evaluate(
    engine: Engine, reference: str, number: int, command: Evaluation, session: str
) -> dict[str, Any]:
    request_hash = hashlib.sha256(
        encoded(
            {"reference": reference, "run": number, "command": command.model_dump(mode="json")}
        ).encode()
    ).hexdigest()
    with engine.begin() as conn:
        execute(
            conn, "SELECT pg_advisory_xact_lock(hashtext(:key))", key=str(command.idempotency_key)
        )
        old = row(
            conn,
            "SELECT * FROM asr_evaluation_events WHERE id=:id",
            id=str(command.idempotency_key),
        )
        if old:
            if old["request_hash"] != request_hash:
                raise VoiceError("Ключ уже использован", "conflict", 409)
            return old["after_data"]
        row(conn, "SELECT reference FROM submissions WHERE reference=:id FOR UPDATE", id=reference)
        current = row(
            conn,
            "SELECT * FROM asr_evaluations WHERE submission=:id AND run=:n",
            id=reference,
            n=number,
        )
        if (current["revision"] if current else 0) != command.revision:
            raise VoiceError("Оценка изменилась", "conflict", 409)
        # Same transaction/connection for candidate identity, no network request.
        candidates = [
            dict(r)
            for r in conn.execute(
                text(
                    "SELECT * FROM asr_candidates WHERE submission=:id AND run=:n ORDER BY engine"
                ),
                {"id": reference, "n": number},
            ).mappings()
        ]
        states = [
            dict(r)
            for r in conn.execute(
                text(
                    "SELECT kind,state,error,request FROM stages WHERE submission=:id AND run=:n AND kind<>'analysis' ORDER BY kind"
                ),
                {"id": reference, "n": number},
            ).mappings()
        ]
        digest = hashlib.sha256(
            encoded({"candidates": candidates, "states": states}).encode()
        ).hexdigest()
        if digest != command.candidates_digest:
            raise VoiceError("Результаты распознавания изменились", "conflict", 409)
        if command.preferred_engine and command.preferred_engine not in {
            c["engine"] for c in candidates
        }:
            raise VoiceError("Нет успешного результата выбранной модели")
        if command.reference_text is not None and not normalize(command.reference_text):
            raise VoiceError("Эталонный текст пуст")
        after = {
            "preferred_engine": command.preferred_engine,
            "reference_text": command.reference_text,
            "note": command.note,
            "revision": command.revision + 1,
        }
        execute(
            conn,
            """INSERT INTO asr_evaluations(submission,run,revision,preferred_engine,reference_text,note)
        VALUES(:id,:n,:revision,:preferred_engine,:reference_text,:note) ON CONFLICT(submission,run) DO UPDATE
        SET revision=excluded.revision,preferred_engine=excluded.preferred_engine,reference_text=excluded.reference_text,note=excluded.note""",
            id=reference,
            n=number,
            **after,
        )
        execute(
            conn,
            "INSERT INTO asr_evaluation_events(id,submission,run,session_id,request_hash,before_data,after_data) VALUES(:id,:ref,:n,:session,:hash,CAST(:before AS jsonb),CAST(:after AS jsonb))",
            id=str(command.idempotency_key),
            ref=reference,
            n=number,
            session=str(UUID(session)),
            hash=request_hash,
            before=encoded(current or {}),
            after=encoded(after),
        )
        return after
