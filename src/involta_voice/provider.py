import json
from typing import Any

import httpx

from .config import Settings


class ProviderFailure(Exception):
    def __init__(self, message: str, *, unknown: bool = False, retry: bool = False) -> None:
        super().__init__(message)
        self.unknown = unknown
        self.retry = retry


class OpenAIProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = httpx.Client(timeout=httpx.Timeout(90, connect=15))

    def call(self, kind: str, request: dict[str, Any], audio: bytes, mime: str) -> dict[str, Any]:
        if kind != "analysis":
            raise ProviderFailure("Аудио распознаётся только локальным ASR")
        key = self.settings.openai_api_key.get_secret_value()
        if not key:
            raise ProviderFailure("OPENAI_API_KEY не настроен")
        headers = {"Authorization": "Bearer " + key}
        try:
            response = self.client.post(
                "https://api.openai.com/v1/responses", headers=headers, json=request
            )
        except httpx.HTTPError as exc:
            raise ProviderFailure(
                "Сетевой исход OpenAI неизвестен; автоматический повтор остановлен", unknown=True
            ) from exc
        return {
            "status_code": response.status_code,
            "request_id": response.headers.get("x-request-id"),
            "body": response.text,
        }

    def close(self) -> None:
        self.client.close()


class FakeProvider:
    """Explicit local/test provider; never contacts a paid API."""

    def call(self, kind: str, request: dict[str, Any], audio: bytes, mime: str) -> dict[str, Any]:
        body: dict[str, Any]
        if kind == "transcription":
            body = {"text": "Был здесь сегодня, было вкусно."}
        else:
            body = {
                "id": "fake_response",
                "status": "completed",
                "output": [
                    {
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "outcome": "no_action",
                                        "reason": "Нужна ручная привязка ресторана",
                                        "entries": [],
                                        "suggestions": [],
                                    }
                                ),
                            }
                        ]
                    }
                ],
            }
        return {"status_code": 200, "request_id": "fake-no-paid-request", "body": json.dumps(body)}

    def close(self) -> None:
        pass


class VoiceProvider:
    def __init__(self, settings: Settings) -> None:
        from .asr import LocalAsr

        self.asr = LocalAsr(settings)
        self.analysis = OpenAIProvider(settings)

    def call(self, kind: str, request: dict[str, Any], audio: bytes, mime: str) -> dict[str, Any]:
        if kind == "transcription":
            return self.asr.call(request, audio)
        return self.analysis.call(kind, request, b"", mime)

    def close(self) -> None:
        self.analysis.close()
