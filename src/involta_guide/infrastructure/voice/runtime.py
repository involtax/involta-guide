"""Production Voice API with Guide delivery in the same container.

The adapter owns Guide credentials and imports. The standalone Voice application
and its database stay independent of the Guide schema.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from threading import Thread

from fastapi import FastAPI

from involta_guide.config import Settings
from involta_guide.infrastructure.persistence.unit_of_work import create_database
from involta_voice.config import Settings as VoiceSettings
from involta_voice.main import create_app as voice_app

from .client import VoiceClient
from .consumer import Consumer


def deliver(consumer: Consumer) -> None:
    backoff = 3
    while not consumer.stopped.is_set():
        try:
            worked = consumer.once()
            backoff = 3
        except Exception:
            # once() records a sanitized diagnostic; the durable inbox/outbox
            # retains unacknowledged work for the next iteration or process.
            worked = 0
            backoff = min(60, backoff * 2)
        if not worked:
            consumer.stopped.wait(backoff)


def create_app(
    settings: VoiceSettings | None = None, guide_settings: Settings | None = None
) -> FastAPI:
    app = voice_app(settings)
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        guide = guide_settings or Settings()
        engine = create_database(guide)
        consumer = Consumer(engine, VoiceClient(guide))
        thread = Thread(target=deliver, args=(consumer,), name="voice-delivery", daemon=True)
        async with original_lifespan(app):
            app.state.consumer = consumer
            thread.start()
            try:
                yield
            finally:
                consumer.stopped.set()
                # HTTP and SQL operations have timeouts. Keep the event loop alive
                # while an in-flight local API request finishes or disconnects.
                await asyncio.to_thread(thread.join, 65)
                if thread.is_alive():
                    logging.error(
                        "Voice delivery did not stop within 65s; pending events remain durable"
                    )
                    raise RuntimeError("Voice delivery shutdown timeout")
                engine.dispose()

    app.router.lifespan_context = lifespan
    return app
