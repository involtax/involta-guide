"""Operator beta inventory and aggregate results. Never emits domain events."""

import hashlib
from statistics import median
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import Engine, text

from .asr import MODELS
from .db import encoded, snapshot
from .evaluation import distance, normalize, queue_evaluation, report
from .service import VoiceService


def backfill(engine: Engine, settings: Any, execute: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = dict(
        mode="execute" if execute else "dry-run",
        checked=0,
        enqueued=0,
        reused=0,
        technical_skipped=0,
        errors=[],
    )
    with engine.connect() as conn:
        version = encoded(snapshot(conn, settings)["asr_models"])
    after = None
    storage = VoiceService(engine, settings).storage
    while True:
        with engine.connect() as conn:
            items = (
                conn.execute(
                    text("""SELECT reference,audio,sha256,metadata FROM submissions
                WHERE (CAST(:after AS uuid) IS NULL OR reference>CAST(:after AS uuid))
                ORDER BY reference LIMIT 100"""),
                    {"after": after},
                )
                .mappings()
                .all()
            )
        if not items:
            break
        for item in items:
            reference = str(item["reference"])
            after = reference
            agent = item["metadata"].get("user_agent", "")
            if agent.startswith(("deployment-smoke", "voice-smoke")):
                result["technical_skipped"] += 1
                continue
            try:
                content = storage.path(item["audio"]).read_bytes()
                if hashlib.sha256(content).hexdigest() != item["sha256"]:
                    raise ValueError("audio_sha256_mismatch")
                result["checked"] += 1
                if execute:
                    key = str(uuid5(NAMESPACE_URL, "asr-beta-v1:" + reference + ":" + version))
                    outcome = queue_evaluation(engine, settings, reference, key)
                    result["reused" if outcome.get("reused") else "enqueued"] += 1
            except (OSError, ValueError) as exc:
                result["errors"].append({"reference": reference, "error": type(exc).__name__})
    return result


def summary(engine: Engine, settings: Any) -> dict[str, Any]:
    """Latest pair per recording for the current pinned configuration; fakes excluded."""
    with engine.connect() as conn:
        models = snapshot(conn, settings)["asr_models"]
        selected = (
            conn.execute(
                text("""SELECT DISTINCT ON (r.submission) r.submission,r.number
          FROM runs r JOIN submissions s ON s.reference=r.submission
          WHERE r.snapshot->'asr_models'=CAST(:models AS jsonb)
          AND coalesce(r.snapshot->>'fake_provider','false')<>'true'
          AND (SELECT count(*) FROM stages j WHERE j.submission=r.submission AND j.run=r.number
               AND j.kind<>'analysis')=2
          AND NOT EXISTS(SELECT 1 FROM asr_candidates c WHERE c.submission=r.submission
                         AND c.run=r.number AND c.details->>'is_fake'='true')
          AND coalesce(s.metadata->>'user_agent','') NOT LIKE 'deployment-smoke%%'
          AND coalesce(s.metadata->>'user_agent','') NOT LIKE 'voice-smoke%%'
          ORDER BY r.submission,r.number DESC"""),
                {"models": encoded(models)},
            )
            .mappings()
            .all()
        )
    totals: dict[str, Any] = {
        name: dict(
            success=0,
            failed=0,
            pending=0,
            votes=0,
            reference_recordings=0,
            word_errors=0,
            reference_words=0,
            character_errors=0,
            reference_characters=0,
            seconds=0.0,
            audio_seconds=0.0,
            rtfs=[],
        )
        for name in MODELS
    }
    annotated = 0
    for entry in selected:
        trial = next(
            r
            for r in report(engine, str(entry["submission"]))["runs"]
            if r["run"] == entry["number"]
        )
        evaluation = trial["evaluation"] or {}
        annotated += bool(evaluation)
        for candidate in trial["candidates"]:
            item = totals[candidate["engine"]]
            if candidate["state"] == "completed" and candidate["text"]:
                item["success"] += 1
                if candidate["rtf"] is not None and candidate["rtf"] > 0:
                    item["seconds"] += candidate["seconds"]
                    item["audio_seconds"] += candidate["seconds"] / candidate["rtf"]
                    item["rtfs"].append(candidate["rtf"])
                truth = normalize(evaluation.get("reference_text") or "")
                if truth:
                    hypothesis = normalize(candidate["text"])
                    item["reference_recordings"] += 1
                    item["word_errors"] += distance(truth.split(), hypothesis.split())
                    item["reference_words"] += len(truth.split())
                    item["character_errors"] += distance(
                        truth.replace(" ", ""), hypothesis.replace(" ", "")
                    )
                    item["reference_characters"] += len(truth.replace(" ", ""))
            elif candidate["state"] in ("failed", "unknown"):
                item["failed"] += 1
            else:
                item["pending"] += 1
            item["votes"] += evaluation.get("preferred_engine") == candidate["engine"]
    for item in totals.values():
        item["wer"] = (
            item["word_errors"] / item["reference_words"] if item["reference_words"] else None
        )
        item["cer"] = (
            item["character_errors"] / item["reference_characters"]
            if item["reference_characters"]
            else None
        )
        item["rtf"] = item["seconds"] / item["audio_seconds"] if item["audio_seconds"] else None
        rtfs = item.pop("rtfs")
        item["median_rtf"] = median(rtfs) if rtfs else None
    return dict(
        models=models,
        recordings=len(selected),
        annotated_recordings=annotated,
        selection="latest_pair_per_recording_current_model_settings_excluding_fakes",
        engines=totals,
    )
