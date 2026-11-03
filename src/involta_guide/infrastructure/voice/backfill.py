"""Idempotent ASR-only backfill of existing Guide audio, including trusted commands."""

import argparse
import hashlib
import json
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import text

from involta_guide.config import Settings
from involta_guide.domain.entities import RuleViolation
from involta_guide.infrastructure.persistence.unit_of_work import create_database
from involta_guide.infrastructure.storage.audio import FileAudioStorage
from involta_voice.contracts import LocationContext, SubmissionMetadata
from involta_voice.db import execute, row

from .client import VoiceClient


def source(engine: Any, review_id: str) -> dict[str, Any]:
    with engine.connect() as conn:
        item = row(
            conn,
            "SELECT r.*,l.slug,l.name location_name,t.original_text,t.editorial_text FROM reviews r JOIN locations l ON l.id=r.location_id LEFT JOIN review_texts t ON t.review_id=r.id WHERE r.id=:id",
            id=review_id,
        )
    if not item:
        raise RuleViolation("Отзыв не найден")
    return item


def audio(settings: Settings, item: dict[str, Any]) -> bytes:
    data = FileAudioStorage(settings.data_dir, settings.max_audio_bytes).read(item["audio"])
    if hashlib.sha256(data).hexdigest() != item["hash"]:
        raise RuleViolation("SHA исходной аудиозаписи не совпадает")
    return data


def ensure_copy(settings: Settings, engine: Any, review_id: str, client: VoiceClient) -> str:
    item = source(engine, review_id)
    if item["audio"].startswith("voice:"):
        return str(UUID(item["audio"].split(":", 1)[1]))
    with engine.connect() as conn:
        previous = row(
            conn,
            "SELECT reference,sha256 FROM voice_archives WHERE source_review=:id",
            id=review_id,
        )
    if previous:
        if previous["sha256"] != item["hash"]:
            raise RuleViolation("SHA архивной ссылки не совпадает")
        return str(previous["reference"])
    content = audio(settings, item)
    # Archive identity never gives browser ownership or execution privileges.
    meta = SubmissionMetadata(
        location=LocationContext(
            id=item["location_id"], slug=item["slug"], name=item["location_name"]
        ),
        visitor=hashlib.sha256(("archive:" + item["visitor"]).encode()).hexdigest(),
        ip_fingerprint=hashlib.sha256(item["ip_hash"].encode()).hexdigest(),
        user_agent="asr-beta/history-copy",
        idempotency_key=UUID(review_id),
        correlation_id=uuid4(),
        actor="admin" if item["alex_session_id"] or item["admin_session_id"] else "visitor",
    )
    legacy = {
        k: item[k] for k in ["moderation", "fraud", "result", "original_text", "editorial_text"]
    }
    legacy.update(sha256=item["hash"], source_review=review_id, created_at=str(item["created_at"]))
    result = client.json(
        "POST",
        "/admin/archive",
        data={
            "source_review": review_id,
            "metadata": meta.model_dump_json(),
            "legacy": json.dumps(legacy, ensure_ascii=False, default=str),
        },
        files={"audio": ("recording", content, item["mime"])},
    )
    if result["sha256"] != item["hash"]:
        raise RuleViolation("SHA скопированной записи не совпадает")
    with engine.begin() as conn:
        execute(
            conn,
            "INSERT INTO voice_archives(source_review,reference,sha256) VALUES(:source,:reference,:sha) ON CONFLICT(source_review) DO NOTHING",
            source=review_id,
            reference=result["reference"],
            sha=item["hash"],
        )
    return str(result["reference"])


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare all retained real Guide recordings with both ASR models; no review/command application"
    )
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    engine = create_database(settings)
    client = VoiceClient(settings)
    report: dict[str, Any] = {
        "mode": "execute" if args.execute else "dry-run",
        "checked": 0,
        "enqueued": 0,
        "reused": 0,
        "missing_audio": 0,
        "sha_mismatch": 0,
        "technical_skipped": 0,
        "errors": [],
    }
    try:
        config = client.json("GET", "/admin/settings") if args.execute else {}
        version = json.dumps(config.get("asr_models", {}), sort_keys=True)
        # No arbitrary LIMIT: walk the complete retained Guide inventory in stable batches.
        after = None
        while True:
            with engine.connect() as conn:
                items = [
                    dict(r)
                    for r in conn.execute(
                        text(
                            "SELECT id,audio,hash,device FROM reviews WHERE (CAST(:after AS uuid) IS NULL OR id>CAST(:after AS uuid)) ORDER BY id LIMIT 100"
                        ),
                        {"after": after},
                    ).mappings()
                ]
            if not items:
                break
            for item in items:
                identity = str(item["id"])
                after = identity
                if str(item["device"]).startswith("deployment-smoke"):
                    report["technical_skipped"] += 1
                    continue
                try:
                    if not item["audio"].startswith("voice:"):
                        audio(settings, source(engine, identity))
                    report["checked"] += 1
                    if args.execute:
                        reference = ensure_copy(settings, engine, identity, client)
                        key = str(uuid5(NAMESPACE_URL, "asr-beta-v1:" + reference + ":" + version))
                        result = client.json(
                            "POST",
                            "/admin/submissions/" + reference + "/asr-runs",
                            json={"idempotency_key": key},
                        )
                        report["reused" if result.get("reused") else "enqueued"] += 1
                except FileNotFoundError:
                    report["missing_audio"] += 1
                except RuleViolation as exc:
                    report["sha_mismatch" if "SHA" in str(exc) else "missing_audio"] += 1
                except Exception as exc:
                    report["errors"].append({"review_id": identity, "type": type(exc).__name__})
        print(json.dumps(report, ensure_ascii=False))
        if report["errors"] or report["sha_mismatch"]:
            raise SystemExit(1)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
