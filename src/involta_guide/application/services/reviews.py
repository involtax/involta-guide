import hashlib
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from involta_guide.application.ports.repositories import AudioStorage, Repository, UowFactory
from involta_guide.domain.entities import Conflict, RuleViolation

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
        idempotency_key: str | None = None,
    ) -> str:
        digest = hashlib.sha256(content).hexdigest()
        key = (
            str(
                uuid5(
                    NAMESPACE_URL,
                    "guide-voice-upload:" + visitor + ":" + str(UUID(idempotency_key)),
                )
            )
            if idempotency_key
            else str(uuid4())
        )

        def replay(repo: Repository) -> str | None:
            old = repo.get("reviews", key) if idempotency_key else None
            if old is None:
                return None
            if (
                str(old["location_id"]) != str(location_id)
                or old["visitor"] != visitor
                or old["hash"] != digest
                or old["mime"] != mime
                or bool(old["alex_session_id"] or old["admin_session_id"])
                != bool(alex_session or admin_session)
            ):
                raise Conflict("Ключ отправки уже использован для другой записи")
            return key

        with self.uow() as uow:
            previous = replay(uow.repo)
            if previous:
                return previous
            location = require(uow.repo, "locations", location_id)
            if not location["active"]:
                raise RuleViolation("Локация не найдена")
            uow.repo.rate_limit("audio-visitor:" + visitor, 6, 900)
            uow.repo.rate_limit("audio-ip:" + ip_hash, 30, 3600)
        name, duration = self.storage.save(content, mime)
        try:
            with self.uow() as uow:
                repo = uow.repo
                # guard_pending serializes intake. Recheck under that same lock so
                # concurrent retries cannot enqueue the administrator command twice.
                try:
                    repo.guard_pending(self.pending_limit)
                except RuleViolation:
                    previous = replay(repo)
                    if previous:
                        self.storage.delete(name)
                        return previous
                    raise
                previous = replay(repo)
                if previous:
                    self.storage.delete(name)
                    return previous
                repo.add(
                    "reviews",
                    {
                        "id": key,
                        "location_id": location_id,
                        "visitor": visitor,
                        "audio": name,
                        "mime": mime,
                        "hash": digest,
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
