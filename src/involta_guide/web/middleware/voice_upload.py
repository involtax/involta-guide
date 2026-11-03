import hashlib
import logging
import time
import traceback
from typing import Any
from uuid import UUID, uuid4

import anyio
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from involta_guide.infrastructure.security import Security
from involta_guide.infrastructure.voice.upload_debug import UploadDebug


class VoiceUploadTrace:
    def __init__(
        self, app: ASGIApp, store: UploadDebug, security: Security, prefix: str, enabled: bool
    ) -> None:
        self.app, self.store, self.security = app, store, security
        self.paths = {
            prefix + "/api/reviews",
            prefix + "/voice/api/v1/submissions",
            prefix + "/voice/api/v1/identity",
        }
        self.enabled = enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] not in self.paths or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        try:
            request_id = str(UUID(request.headers.get("x-voice-request-id", "")))
        except ValueError:
            request_id = str(uuid4())
        scope.setdefault("state", {})["voice_request_id"] = request_id
        owner_cookie = request.cookies.get("guide_visitor", "")
        owner = (
            self.security.fingerprint("voice-upload-debug:" + owner_cookie) if owner_cookie else ""
        )
        private = (
            *request.cookies.values(),
            request.headers.get("authorization", ""),
            request.headers.get("x-csrf-token", ""),
            *(
                self.security.csrf(request.cookies[key])
                for key in ("guide_admin", "guide_alex")
                if request.cookies.get(key)
            ),
        )
        start = time.monotonic()
        received = 0
        digest = hashlib.sha256()
        status = 0
        response_body: list[bytes] = []
        response_headers: dict[str, str] = {}
        finished = False
        exception = ""

        async def record(event: dict[str, Any]) -> None:
            if not self.enabled:
                return
            try:
                with anyio.CancelScope(shield=True):
                    await run_in_threadpool(
                        self.store.append, request_id, {"owner": owner, **event}, private
                    )
            except Exception:
                logging.getLogger(__name__).exception(
                    "Cannot persist upload diagnostic %s", request_id
                )

        await record(
            {
                "stage": "received",
                "path": scope["path"],
                "method": scope["method"],
                "content_type": request.headers.get("content-type"),
                "content_length": request.headers.get("content-length"),
            }
        )

        async def counted_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                received += len(body)
                digest.update(body)
            return message

        async def traced_send(message: Message) -> None:
            nonlocal status, finished
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-voice-request-id", request_id.encode()))
                message = {**message, "headers": headers}
                response_headers.update(
                    {
                        key.decode(): value.decode()
                        for key, value in headers
                        if key.lower()
                        in (b"content-type", b"content-length", b"x-voice-request-id")
                    }
                )
            elif message["type"] == "http.response.body":
                if self.enabled:
                    response_body.append(message.get("body", b""))
                finished = not message.get("more_body", False)
            await send(message)

        try:
            await self.app(scope, counted_receive, traced_send)
        except Exception:
            exception = traceback.format_exc()
            raise
        finally:
            await record(
                {
                    "stage": "response" if finished else "interrupted",
                    "path": scope["path"],
                    "status": status or 500,
                    "elapsed_ms": round((time.monotonic() - start) * 1000),
                    "received_bytes": received,
                    "request_sha256": digest.hexdigest(),
                    "response_headers": response_headers,
                    "response_body": b"".join(response_body).decode("utf-8", errors="replace"),
                    "exception": exception,
                }
            )
