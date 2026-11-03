import logging
import signal
import threading
import traceback
from typing import Any

from pydantic import ValidationError

from involta_guide.application.services.jobs import JobService
from involta_guide.config import Settings
from involta_guide.domain.entities import Conflict, RuleViolation
from involta_guide.infrastructure.ai.audit import AIAudit
from involta_guide.infrastructure.ai.errors import GenerationPending, ProviderUnavailable
from involta_guide.infrastructure.ai.openai import OpenAIProvider
from involta_guide.infrastructure.persistence.unit_of_work import PostgresUow, create_database

from .queue import JobQueue


class Worker:
    def __init__(self, queue: JobQueue, service: JobService) -> None:
        self.queue = queue
        self.service = service
        if service.reset_handler is None:
            from involta_guide.infrastructure.beta_reset import BetaReset

            service.reset_handler = BetaReset(queue.engine, queue.settings, service).perform
        self.stop = threading.Event()
        self.audit = AIAudit(queue.engine, queue.settings)

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
            if not job.get("background_resume"):
                self.audit.record(
                    job["id"], "job_started", {"attempt": job["attempts"], "job": job}
                )
            self.service.process(job)
            self.audit.record(
                job["id"],
                "job_completed",
                {
                    "attempt": job["attempts"],
                },
            )
        except GenerationPending as exc:
            self.queue.defer(job, str(exc))
        except (RuleViolation, Conflict, ValidationError) as exc:
            self.audit.record(
                job["id"],
                "job_failed",
                {
                    "attempt": job["attempts"],
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                    "validation_errors": exc.errors(include_url=False)
                    if isinstance(exc, ValidationError)
                    else None,
                },
            )
            message = (
                "AI response validation failed" if isinstance(exc, ValidationError) else str(exc)
            )
            self.queue.fail(job, message, retry=False)
        except ProviderUnavailable as exc:
            self.audit.record(
                job["id"],
                "job_failed",
                {
                    "attempt": job["attempts"],
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                },
            )
            self.queue.fail(job, str(exc), retry=False)
        except Exception:
            self.audit.record(
                job["id"],
                "job_failed",
                {
                    "attempt": job["attempts"],
                    "traceback": traceback.format_exc(),
                },
            )
            self.queue.fail(
                job,
                "Internal processing error; inspect server diagnostics",
                retry=job["kind"] == "reset",
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
    provider = OpenAIProvider(settings, engine)
    worker = Worker(
        JobQueue(engine, settings),
        JobService(
            lambda: PostgresUow(engine),
            provider,
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
