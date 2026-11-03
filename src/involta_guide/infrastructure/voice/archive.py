import argparse
import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

from involta_guide.config import Settings
from involta_guide.infrastructure.persistence.unit_of_work import create_database
from involta_guide.infrastructure.storage.audio import FileAudioStorage
from involta_voice.contracts import LocationContext, SubmissionMetadata
from involta_voice.db import execute, row

from .client import VoiceClient


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify/copy legacy audio without moving processing ownership"
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()
    settings = Settings()
    engine = create_database(settings)
    storage = FileAudioStorage(settings.data_dir, settings.max_audio_bytes)
    client = VoiceClient(settings)
    report: dict[str, Any] = {
        "mode": "execute" if args.execute else "dry-run",
        "checked": 0,
        "copied": 0,
        "skipped": 0,
        "sha_mismatch": 0,
        "missing_audio": 0,
    }
    try:
        from sqlalchemy import text

        with engine.connect() as conn:
            reviews = [
                dict(r)
                for r in conn.execute(
                    text("""SELECT r.*,l.slug,l.name location_name,
                t.original_text,t.editorial_text FROM reviews r JOIN locations l ON l.id=r.location_id
                LEFT JOIN review_texts t ON t.review_id=r.id ORDER BY r.created_at,r.id LIMIT :limit"""),
                    {"limit": max(1, args.limit)},
                ).mappings()
            ]
        for review in reviews:
            if (
                review["alex_session_id"]
                or review["admin_session_id"]
                or review["deleted_at"]
                or review["audio"].startswith("voice:")
            ):
                report["skipped"] += 1
                continue
            with engine.connect() as conn:
                active = row(
                    conn,
                    "SELECT 1 FROM jobs WHERE review_id=:id AND status IN ('queued','running')",
                    id=review["id"],
                )
            if active:
                report["skipped"] += 1
                continue
            try:
                content = storage.read(review["audio"])
            except OSError:
                report["missing_audio"] += 1
                continue
            digest = hashlib.sha256(content).hexdigest()
            report["checked"] += 1
            if digest != review["hash"]:
                report["sha_mismatch"] += 1
                continue
            if not args.execute:
                continue
            # Opaque identity cannot be turned into a browser cookie. Copy is read-only.
            visitor = hashlib.sha256(("archive:" + review["visitor"]).encode()).hexdigest()
            metadata = SubmissionMetadata(
                location=LocationContext(
                    id=review["location_id"], slug=review["slug"], name=review["location_name"]
                ),
                visitor=visitor,
                ip_fingerprint=review["ip_hash"]
                if len(review["ip_hash"]) >= 32
                else hashlib.sha256(review["ip_hash"].encode()).hexdigest(),
                user_agent=review["device"][:500],
                idempotency_key=UUID(str(review["id"])),
                correlation_id=uuid4(),
            )
            legacy = {
                key: review[key]
                for key in ("moderation", "fraud", "result", "original_text", "editorial_text")
            }
            legacy.update(
                sha256=digest, source_review=str(review["id"]), created_at=str(review["created_at"])
            )
            result = client.json(
                "POST",
                "/admin/archive",
                data={
                    "source_review": str(review["id"]),
                    "metadata": metadata.model_dump_json(),
                    "legacy": json.dumps(legacy, ensure_ascii=False, default=str),
                },
                files={"audio": ("recording", content, review["mime"])},
            )
            if result["sha256"] != digest:
                raise ValueError("Archive verification failed")
            with engine.begin() as conn:
                execute(
                    conn,
                    "INSERT INTO voice_archives(source_review,reference,sha256) VALUES(:source,:reference,:hash) ON CONFLICT(source_review) DO NOTHING",
                    source=str(review["id"]),
                    reference=result["reference"],
                    hash=digest,
                )
            report["copied"] += 1
        print(json.dumps(report, ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
