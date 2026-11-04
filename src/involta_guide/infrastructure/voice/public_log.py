"""Public beta diagnostics: processing data without visitor credentials/metadata."""

import json
import re
from typing import Any


def public_log(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        private = {
            "visitor",
            "owner",
            "ip",
            "user_agent",
            "metadata",
            "audio",
            "audio_url",
            "idempotency_key",
            "session_id",
            "actor_session",
            "headers",
            "cookie",
            "cookies",
            "authorization",
            "password",
            "token",
            "api_key",
            "secret",
        }
        return {
            k: public_log(v, secrets)
            for k, v in value.items()
            if k.lower() not in private
            and not any(s in k.lower() for s in ("password", "secret", "api_key"))
        }
    if isinstance(value, list):
        return [public_log(v, secrets) for v in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            return public_log(parsed, secrets)
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
        return re.sub(r"(?i)(?:bearer\s+[^\s\"']+|sk-[a-zA-Z0-9_-]+)", "[redacted]", value)
    return value
