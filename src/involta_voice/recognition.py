"""Private recognition results and batch reads for the administrator feed."""

from typing import Any

from sqlalchemy import Engine, text

from .asr import MODELS
from .db import encoded, execute, row
from .errors import VoiceError
from .variants import RESULTS, VARIANT_LABELS, primary_variant, result_identity


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
        """INSERT INTO asr_candidates(submission,run,engine,variant,revision,settings,transcript,elapsed_seconds,duration,details)
    VALUES(:id,:run,:engine,:variant,:revision,CAST(:settings AS jsonb),:text,:elapsed,:duration,CAST(:details AS jsonb))
    ON CONFLICT(submission,run,engine,variant) DO NOTHING""",
        id=reference,
        run=run,
        engine=engine,
        variant=request.get("variant", "onnx"),
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
                text(
                    "SELECT s.reference,s.transcript,s.active_run,r.snapshot FROM submissions s JOIN runs r ON r.submission=s.reference AND r.number=s.active_run WHERE s.reference=ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": references},
            )
            .mappings()
            .all()
        )
        candidates = (
            conn.execute(
                text(
                    "SELECT submission,run,engine,variant,settings,transcript FROM asr_candidates WHERE submission=ANY(CAST(:ids AS uuid[])) AND engine='gigaam' ORDER BY run DESC"
                ),
                {"ids": references},
            )
            .mappings()
            .all()
        )
        stages = (
            conn.execute(
                text(
                    "SELECT s.submission,s.run,s.kind,s.state,s.error,coalesce(s.request,r.snapshot->'sherpa_models'->s.kind) AS request,r.snapshot->>'transcription_model' AS primary_engine FROM stages s JOIN runs r ON r.submission=s.submission AND r.number=s.run WHERE s.submission=ANY(CAST(:ids AS uuid[])) AND (s.kind IN ('transcription','comparison','evaluate_gigaam') OR s.kind LIKE 'sherpa_gigaam_%') ORDER BY s.run DESC"
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
        # Read old controls only when their original job/result actually exists.
        for name, variant in (("gigaam", "onnx"), *RESULTS):
            candidate = next(
                (
                    c
                    for c in candidates
                    if str(c["submission"]) == ref
                    and c["engine"] == name
                    and c["variant"] == variant
                ),
                None,
            )
            stage = next(
                (
                    s
                    for s in stages
                    if str(s["submission"]) == ref
                    and (
                        (
                            (s["request"] or {}).get("model"),
                            (s["request"] or {}).get("variant", "onnx"),
                        )
                        if s["request"]
                        else result_identity(s["kind"], s["primary_engine"])
                    )
                    == (name, variant)
                ),
                None,
            )
            # Existing reviews retain their saved profiles; no comparison is scheduled by reads.
            if candidate is None and stage is None:
                continue
            current_stage = stage if stage and stage["run"] == submission["active_run"] else None
            request = (
                (current_stage["request"] or {})
                if current_stage
                else candidate["settings"]
                if candidate
                else {}
            )
            pair.append(
                {
                    "engine": name,
                    "variant": variant,
                    "name": "GigaAM",
                    "variant_label": VARIANT_LABELS[variant],
                    "dictionary": request.get("dictionary", []) if stage or candidate else None,
                    "max_active_paths": request.get("max_active_paths"),
                    "hotwords_score": request.get("hotwords_score"),
                    "primary": variant == primary_variant(submission["snapshot"]),
                    "run": submission["active_run"],
                    "review_text_format": submission["snapshot"].get("review_text_format")
                    if variant == primary_variant(submission["snapshot"])
                    else None,
                    "raw_text": candidate["transcript"] if candidate else None,
                    "text": (
                        submission["transcript"]
                        if variant == primary_variant(submission["snapshot"])
                        and submission["snapshot"].get("restore_dictionary_case")
                        else candidate["transcript"]
                    )
                    if candidate
                    else None,
                    "state": current_stage["state"]
                    if current_stage
                    else "completed"
                    if candidate
                    else "not_scheduled",
                    "error": current_stage["error"] if current_stage else None,
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
                text(
                    "SELECT * FROM asr_candidates WHERE submission=:id ORDER BY run DESC,engine,variant"
                ),
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
                    text("""SELECT engine,variant,count(*) recordings,
            sum(elapsed_seconds) seconds,sum(duration) audio_seconds FROM (
            SELECT DISTINCT ON(submission,engine,variant) * FROM asr_candidates
            WHERE engine='gigaam' AND coalesce(details->>'is_fake','false')<>'true'
            ORDER BY submission,engine,variant,run DESC) latest GROUP BY engine,variant ORDER BY engine,variant""")
                ).mappings()
            ]
        }
