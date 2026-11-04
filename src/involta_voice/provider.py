import json
from typing import Any

from .config import Settings


class ProviderFailure(Exception):
    def __init__(self, message: str, *, unknown: bool = False, retry: bool = False) -> None:
        super().__init__(message)
        self.unknown = unknown
        self.retry = retry


class FakeProvider:
    """Explicit local/test provider; never contacts a paid API."""

    def call(self, kind: str, request: dict[str, Any], audio: bytes, mime: str) -> dict[str, Any]:
        body: dict[str, Any]
        if kind == "transcription":
            body = {"text": "Был здесь сегодня, было вкусно."}
        else:
            raise ProviderFailure("Анализ отзывов пока отключён")
        return {"status_code": 200, "request_id": "fake-no-paid-request", "body": json.dumps(body)}

    def close(self) -> None:
        pass


class VoiceProvider:
    def __init__(self, settings: Settings) -> None:
        from .asr import LocalAsr

        self.asr = LocalAsr(settings)
        self.sherpa: Any = None
        self.settings = settings

    def call(self, kind: str, request: dict[str, Any], audio: bytes, mime: str) -> dict[str, Any]:
        if kind == "transcription":
            if request.get("engine") == "sherpa-onnx":
                from .sherpa import LocalSherpa

                self.asr.close()
                if self.sherpa is None:
                    self.sherpa = LocalSherpa(self.settings)
                return self.sherpa.call(request, audio)
            if self.sherpa is not None:
                self.sherpa.close()
            return self.asr.call(request, audio)
        raise ProviderFailure("Анализ отзывов пока отключён")

    def close(self) -> None:
        self.asr.close()
        if self.sherpa is not None:
            self.sherpa.close()
