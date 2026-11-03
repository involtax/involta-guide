import hashlib
from uuid import uuid4

from involta_guide.application.ports.repositories import AudioStorage, UowFactory
from involta_guide.domain.entities import RuleViolation

from .editorial import require


class ReviewService:
    def __init__(self, uow: UowFactory, storage: AudioStorage, pending_limit: int) -> None:
        self.uow = uow
        self.storage = storage
        self.pending_limit = pending_limit

    def submit(
        self,
        location_id: str,
        visitor: str,
        ip_hash: str,
        device: str,
        content: bytes,
        mime: str,
        alex_session: str | None = None,
        admin_session: str | None = None,
    ) -> str:
        with self.uow() as uow:
            location = require(uow.repo, "locations", location_id)
            if not location["active"]:
                raise RuleViolation("Локация не найдена")
            uow.repo.rate_limit("audio-visitor:" + visitor, 6, 900)
            uow.repo.rate_limit("audio-ip:" + ip_hash, 30, 3600)
        name, duration = self.storage.save(content, mime)
        try:
            with self.uow() as uow:
                repo = uow.repo
                repo.guard_pending(self.pending_limit)
                key = str(uuid4())
                repo.add(
                    "reviews",
                    {
                        "id": key,
                        "location_id": location_id,
                        "visitor": visitor,
                        "audio": name,
                        "mime": mime,
                        "hash": hashlib.sha256(content).hexdigest(),
                        "ip_hash": ip_hash,
                        "device": device[:500],
                        "duration_seconds": duration,
                        "alex_session_id": alex_session,
                        "admin_session_id": admin_session,
                    },
                )
                if alex_session or admin_session:
                    repo.add(
                        "alex_changes",
                        {
                            "id": str(uuid4()),
                            "session_id": alex_session,
                            "admin_session_id": admin_session,
                            "review_id": key,
                            "location_id": location_id,
                            "status": "queued",
                            "detail": "Ожидает распознавания",
                        },
                    )
                repo.queue(
                    {
                        "id": str(uuid4()),
                        "kind": "review",
                        "location_id": location_id,
                        "review_id": key,
                    }
                )
                return key
        except Exception:
            self.storage.delete(name)
            raise

    def audio(self, review_id: str, *, alex_only: bool = False) -> tuple[bytes, str]:
        with self.uow() as uow:
            row = require(uow.repo, "reviews", review_id)
            if row["deleted_at"] or (
                alex_only and not (row["alex_session_id"] or row.get("admin_session_id"))
            ):
                raise RuleViolation("Запись недоступна")
            return self.storage.read(row["audio"]), row["mime"]
