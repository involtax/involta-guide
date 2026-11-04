"""Private recognition results and batch reads for the administrator feed."""

from typing import Any

from sqlalchemy import Engine, text

from .asr import MODELS
from .db import encoded, execute, row
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
        raise ValueError("Неизвестная локальная модель")
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


def reports(engine: Engine, references: list[str]) -> dict[str, Any]:
    """Three bounded queries for a feed page, regardless of its review count."""
    if not references:
        return {}
    with engine.connect() as conn:
        submissions = (
            conn.execute(
                text("SELECT reference FROM submissions WHERE reference=ANY(CAST(:ids AS uuid[]))"),
                {"ids": references},
            )
            .mappings()
            .all()
        )
        candidates = (
            conn.execute(
                text(
                    "SELECT submission,run,engine,transcript FROM asr_candidates WHERE submission=ANY(CAST(:ids AS uuid[])) ORDER BY run DESC"
                ),
                {"ids": references},
            )
            .mappings()
            .all()
        )
        stages = (
            conn.execute(
                text(
                    "SELECT s.submission,s.run,s.kind,s.state,s.error,s.request,r.snapshot->>'transcription_model' AS primary_engine FROM stages s JOIN runs r ON r.submission=s.submission AND r.number=s.run WHERE s.submission=ANY(CAST(:ids AS uuid[])) AND s.kind<>'analysis' ORDER BY s.run DESC"
                ),
                {"ids": references},
            )
            .mappings()
            .all()
        )
    result = {}
    for submission in submissions:
        ref = str(submission["reference"])
        pair = []
        for name in MODELS:
            candidate = next(
                (c for c in candidates if str(c["submission"]) == ref and c["engine"] == name), None
            )
            stage = next(
                (
                    s
                    for s in stages
                    if str(s["submission"]) == ref
                    and (
                        (s["request"] or {}).get("model") == name
                        or s["kind"] == "evaluate_" + name
                        or (s["kind"] == "transcription" and s["primary_engine"] == name)
                        or (s["kind"] == "comparison" and s["primary_engine"] != name)
                    )
                ),
                None,
            )
            pair.append(
                {
                    "engine": name,
                    "name": "GigaAM" if name == "gigaam" else "Parakeet",
                    "text": candidate["transcript"] if candidate else None,
                    "state": "completed"
                    if candidate
                    else stage["state"]
                    if stage
                    else "not_scheduled",
                    "error": stage["error"] if stage and not candidate else None,
                }
            )
        result[ref] = pair
    return result


def report(engine: Engine, reference: str) -> dict[str, Any]:
    with engine.connect() as conn:
        if not row(conn, "SELECT 1 FROM submissions WHERE reference=:id", id=reference):
            raise VoiceError("Запись не найдена", "not_found", 404)
        candidates = [
            dict(c)
            for c in conn.execute(
                text("SELECT * FROM asr_candidates WHERE submission=:id ORDER BY run DESC,engine"),
                {"id": reference},
            ).mappings()
        ]
    return {
        "reference": reference,
        "candidates": candidates,
        "pair": reports(engine, [reference])[reference],
    }


def summary(engine: Engine) -> dict[str, Any]:
    with engine.connect() as conn:
        return {
            "engines": [
                dict(r)
                for r in conn.execute(
                    text("""SELECT engine,count(*) recordings,
            sum(elapsed_seconds) seconds,sum(duration) audio_seconds FROM (
            SELECT DISTINCT ON(submission,engine) * FROM asr_candidates
            WHERE coalesce(details->>'is_fake','false')<>'true'
            ORDER BY submission,engine,run DESC) latest GROUP BY engine ORDER BY engine""")
                ).mappings()
            ]
        }
