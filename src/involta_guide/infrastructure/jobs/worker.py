import logging
import signal
import threading
from typing import Any

from pydantic import ValidationError

from involta_guide.application.services.jobs import JobService
from involta_guide.config import Settings
from involta_guide.domain.entities import Conflict, RuleViolation
from involta_guide.infrastructure.ai.openai import OpenAIProvider, ProviderUnavailable
from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow, create_database
from involta_guide.infrastructure.storage.audio import FileAudioStorage

from .queue import JobQueue


class Worker:
    def __init__(self, queue: JobQueue, service: JobService) -> None:
        self.queue = queue
        self.service = service
        self.stop = threading.Event()

    def once(self) -> bool:
        job = self.queue.claim()
        if not job:
            return False
        finished = threading.Event()

        def heartbeat() -> None:
            while not finished.wait(max(1, self.queue.settings.lease_seconds // 3)):
                try:
                    if not self.queue.heartbeat(job):
                        return
                    self.queue.settings.worker_heartbeat_file.touch(mode=0o600)
                except Exception:
                    logging.warning("Worker heartbeat unavailable")

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            self.service.process(job)
        except (RuleViolation, Conflict, ValidationError) as exc:
            message = (
                "AI response validation failed" if isinstance(exc, ValidationError) else str(exc)
            )
            self.queue.fail(job, message, retry=False)
        except ProviderUnavailable as exc:
            self.queue.fail(job, str(exc), retry=not job.get("_temporary_public_fill", False))
        except Exception:
            self.queue.fail(
                job, "Internal processing error; inspect server diagnostics", retry=True
            )
            logging.exception("Worker processing failed")
        finally:
            finished.set()
            thread.join(timeout=2)
        return True

    def run(self) -> None:
        while not self.stop.is_set():
            self.queue.settings.worker_heartbeat_file.touch(mode=0o600)
            if not self.once():
                self.stop.wait(1)


def main() -> None:
    settings = Settings()
    engine = create_database(settings)
    storage = FileAudioStorage(settings.data_dir, settings.max_audio_bytes)
    provider = OpenAIProvider(settings, engine, storage)
    worker = Worker(
        JobQueue(engine, settings),
        JobService(
            lambda: PostgresUow(engine),
            provider,
            storage,
            public_fill_enabled=settings.temporary_public_fill_enabled,
        ),
    )

    def shutdown(*args: Any) -> None:
        worker.stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        worker.run()
    finally:
        provider.close()
        engine.dispose()
