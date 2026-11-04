"""Persistent upload diagnostics. Audio, credentials and cookies never enter the audit log."""

import json
import math
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
        if isinstance(value, float) and not math.isfinite(value):
            return None
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
        scope = event.pop("_context", {})
        owner = event.pop("owner", "")
        data = self.clean({**event, "time": datetime.now(UTC).isoformat()}, private)
        with self.engine.begin() as conn:
            location = None
            if scope.get("slug"):
                location = (
                    conn.execute(
                        text(
                            "SELECT id,content_epoch,reset_pending FROM locations WHERE slug=:slug FOR SHARE"
                        ),
                        {"slug": scope["slug"]},
                    )
                    .mappings()
                    .first()
                )
                if location and (
                    location["reset_pending"]
                    or str(location["content_epoch"]) != str(scope.get("content_epoch"))
                ):
                    return
            conn.execute(
                text(
                    "INSERT INTO audit(id,action,target,detail,created_at,location_id,content_epoch,visitor) VALUES(:id,'voice_upload_http',:target,:detail,now(),:location,:epoch,:owner)"
                ),
                {
                    "id": str(uuid4()),
                    "target": request_id,
                    "location": location["id"] if location else None,
                    "epoch": location["content_epoch"] if location else None,
                    "owner": owner,
                    "detail": json.dumps(data, ensure_ascii=False),
                },
            )

    def read(self, request_id: str, owner: str, admin: bool = False) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        "SELECT detail FROM audit WHERE action='voice_upload_http' AND target=:id AND (:admin OR visitor=:owner) ORDER BY created_at,id"
                    ),
                    {"id": str(UUID(request_id)), "admin": admin, "owner": owner},
                )
                .scalars()
                .all()
            )
        events = [json.loads(r) for r in rows]
        for event in events:
            event.pop("owner", None)
            if not admin:
                event.pop("exception", None)
        return events
