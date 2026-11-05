from typing import Any

import httpx

from involta_guide.config import Settings
from involta_guide.domain.entities import RuleViolation


class VoiceUnavailable(RuleViolation):
    pass


class VoiceClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        self.transport = transport

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        secret = self.settings.voice_service_secret.get_secret_value()
        if len(secret) < 32:
            raise VoiceUnavailable("Голосовой сервис не настроен")
        try:
            actor = kwargs.pop("actor_session", None)
            headers = httpx.Headers(kwargs.pop("headers", {}))
            headers["Authorization"] = "Bearer " + secret
            if actor is not None:
                headers["X-Guide-Session"] = str(actor)
            with httpx.Client(
                base_url=self.settings.voice_url, timeout=30, transport=self.transport
            ) as client:
                response = client.request(
                    method,
                    "/internal/v1" + path,
                    headers=headers,
                    **kwargs,
                )
        except httpx.HTTPError as exc:
            raise VoiceUnavailable(
                "Голосовой сервис временно недоступен. Попробуйте позже."
            ) from exc
        return response

    def json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self.request(method, path, **kwargs)
        if not response.is_success:
            raise VoiceUnavailable(f"Голосовой сервис вернул HTTP {response.status_code}")
        return dict(response.json())
