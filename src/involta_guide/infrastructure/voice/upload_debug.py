"""Persistent upload diagnostics. Audio, credentials and cookies never enter the audit log."""

import json
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import SecretStr
from sqlalchemy import Engine, text

from involta_guide.config import Settings


class UploadDebug:
    def __init__(self, engine: Engine, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings

    def clean(self, value: Any, private: tuple[str, ...] = ()) -> Any:
        if isinstance(value, dict):
            return {
                k: self.clean(v, private)
                for k, v in value.items()
                if not any(
                    key in k.lower()
                    for key in ("authorization", "cookie", "csrf", "password", "api_key", "secret")
                )
            }
        if isinstance(value, (list, tuple)):
            return [self.clean(v, private) for v in value]
        if isinstance(value, str):
            value = re.sub(
                r'("(?:csrf_token|_csrf|authorization|cookie|set-cookie|api_key|password|secret)"\s*:\s*)"(?:[^"\\]|\\.)*"',
                r'\1"[REDACTED]"',
                value,
                flags=re.IGNORECASE,
            )
            secrets = [
                v.get_secret_value()
                for v in self.settings.model_dump().values()
                if isinstance(v, SecretStr)
            ]
            for token in [*secrets, *private]:
                if len(token) >= 4:
                    value = value.replace(token, "[REDACTED]")
        return value

    def append(self, request_id: str, event: dict[str, Any], private: tuple[str, ...] = ()) -> None:
        request_id = str(UUID(request_id))
        data = self.clean({**event, "time": datetime.now(UTC).isoformat()}, private)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO audit(id,action,target,detail,created_at) VALUES(:id,'voice_upload_http',:target,:detail,now())"
                ),
                {
                    "id": str(uuid4()),
                    "target": request_id,
                    "detail": json.dumps(data, ensure_ascii=False),
                },
            )

    def read(self, request_id: str, owner: str, admin: bool = False) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT detail FROM audit WHERE action='voice_upload_http' AND target=:id ORDER BY created_at,id"
                    ),
                    {"id": str(UUID(request_id))},
                )
                .scalars()
                .all()
            )
        events = [json.loads(r) for r in rows]
        if not admin:
            events = [e for e in events if owner and e.get("owner") == owner]
        for event in events:
            event.pop("owner", None)
            if not admin:
                event.pop("exception", None)
        return events
