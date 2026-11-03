import hashlib
from typing import Any
from uuid import UUID, uuid4

from .contracts import SubmissionMetadata
from .db import encoded, execute, row, snapshot
from .errors import VoiceError
from .service import VoiceService


def copy_archive(
    service: VoiceService,
    source_review: UUID,
    metadata: SubmissionMetadata,
    content: bytes,
    mime: str,
    legacy: dict[str, Any],
) -> dict[str, Any]:
    """Read-only archive, not a new review/processing owner; never enqueue paid work."""
    digest = hashlib.sha256(content).hexdigest()
    if legacy.get("sha256") != digest:
        raise VoiceError("SHA исходного файла не совпадает", "conflict", 409)
    with service.engine.connect() as conn:
        old = row(conn, "SELECT * FROM archive_map WHERE source_review=:id", id=str(source_review))
    if old:
        if old["sha256"] != digest:
            raise VoiceError("Архивный источник уже скопирован с другим SHA", "conflict", 409)
        return {
            "source_review": str(source_review),
            "reference": str(old["submission"]),
            "sha256": digest,
        }
    name, duration = service.storage.save(content, mime)
    reference = str(uuid4())
    with service.engine.begin() as conn:
        execute(conn, "SELECT pg_advisory_xact_lock(47032002)")
        old = row(conn, "SELECT * FROM archive_map WHERE source_review=:id", id=str(source_review))
        if old:
            service.storage.path(name).unlink(missing_ok=True)
            if old["sha256"] != digest:
                raise VoiceError("Конфликт SHA архива", "conflict", 409)
            reference = str(old["submission"])
        else:
            execute(
                conn,
                """INSERT INTO submissions(reference,location_id,visitor,idempotency_key,payload_hash,
                metadata,audio,mime,duration,size,sha256,repeat_signals,transcript)
                VALUES(:id,:location,:visitor,:key,:hash,CAST(:meta AS jsonb),:audio,:mime,:duration,:size,
                :hash,'{}'::jsonb,:transcript)""",
                id=reference,
                location=str(metadata.location.id),
                visitor=metadata.visitor,
                key=str(metadata.idempotency_key),
                hash=digest,
                meta=metadata.model_dump_json(),
                audio=name,
                mime=mime,
                duration=duration,
                size=len(content),
                transcript=legacy.get("original_text"),
            )
            frozen = {
                **snapshot(conn, service.settings),
                "location": metadata.location.model_dump(mode="json"),
                "archive": legacy,
            }
            execute(
                conn,
                "INSERT INTO runs(submission,number,snapshot,transcript) VALUES(:id,1,CAST(:snapshot AS jsonb),:transcript)",
                id=reference,
                snapshot=encoded(frozen),
                transcript=legacy.get("original_text"),
            )
            execute(
                conn,
                "INSERT INTO archive_map(source_review,submission,sha256) VALUES(:source,:id,:hash)",
                source=str(source_review),
                id=reference,
                hash=digest,
            )
    return {"source_review": str(source_review), "reference": reference, "sha256": digest}
