"""Offline Russian speech recognition. Model download is a separate explicit operation."""

import io
import json
import threading
import time
from pathlib import Path
from typing import Any

from .config import Settings
from .provider import ProviderFailure

MODELS = {
    "large-v3": ("Systran/faster-whisper-large-v3", "edaa852ec7e145841d8ffdb056a99866b5f0a478"),
    "large-v3-russian": (
        "bzikst/faster-whisper-large-v3-russian-int8",
        "157ceddec5214ab3f5f710ae8e377058807d2e97",
    ),
}


def options(settings: Settings) -> dict[str, Any]:
    repository, revision = MODELS[settings.transcription_model]
    return {
        "engine": "faster-whisper",
        "model": settings.transcription_model,
        "revision": revision,
        "device": settings.whisper_device,
        "compute_type": settings.whisper_compute_type,
        "cpu_threads": settings.whisper_cpu_threads,
        "beam_size": 5,
        "language": "ru",
        "vad_filter": True,
        "condition_on_previous_text": False,
    }


def model_path(settings: Settings, request: dict[str, Any] | None = None) -> Path:
    request = request or options(settings)
    return settings.whisper_models_dir / request["model"] / request["revision"]


def prepare(settings: Settings) -> Path:
    from huggingface_hub import snapshot_download

    repository, revision = MODELS[settings.transcription_model]
    target = model_path(settings)
    target.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repository,
        revision=revision,
        local_dir=target,
        allow_patterns=[
            "model.bin",
            "config.json",
            "tokenizer.json",
            "preprocessor_config.json",
            "vocabulary.*",
        ],
    )
    required = ["model.bin", "config.json", "tokenizer.json"]
    if not all((target / name).is_file() for name in required):
        raise RuntimeError("Неполная загрузка модели Whisper")
    # Only mark ready after every file downloaded; serving never downloads weights.
    (target / "READY.json").write_text(json.dumps({"repository": repository, "revision": revision}))
    return target


class LocalWhisper:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._model: Any = None
        self._signature: tuple[Any, ...] | None = None
        self._lock = threading.RLock()

    def warmup(self, request: dict[str, Any] | None = None) -> None:
        request = request or options(self.settings)
        path = model_path(self.settings, request)
        if not (path / "READY.json").is_file():
            raise ProviderFailure("Модель Whisper не подготовлена: выполните voice prepare-model")
        with self._lock:
            signature = (
                request["model"],
                request["revision"],
                request["device"],
                request["compute_type"],
                request["cpu_threads"],
            )
            if self._model is None or self._signature != signature:
                from faster_whisper import WhisperModel

                self._model = WhisperModel(
                    str(path),
                    device=request["device"],
                    compute_type=request["compute_type"],
                    cpu_threads=request["cpu_threads"],
                    num_workers=1,
                    local_files_only=True,
                )
                self._signature = signature

    def call(self, request: dict[str, Any], audio: bytes) -> dict[str, Any]:
        if (
            request.get("engine") != "faster-whisper"
            or request.get("model") not in MODELS
            or request.get("revision") != MODELS.get(str(request.get("model")), ("", ""))[1]
        ):
            raise ProviderFailure("Нужна новая обработка с локальной моделью Whisper")
        path = model_path(self.settings, request)
        if not (path / "READY.json").is_file():
            raise ProviderFailure("Модель Whisper не подготовлена: выполните voice prepare-model")
        started = time.monotonic()
        try:
            with self._lock:
                self.warmup(request)
                segments, info = self._model.transcribe(
                    io.BytesIO(audio),
                    language="ru",
                    task="transcribe",
                    beam_size=request["beam_size"],
                    vad_filter=True,
                    condition_on_previous_text=False,
                    word_timestamps=False,
                    vad_parameters={"min_silence_duration_ms": 500},
                )
                saved = [
                    {
                        "start": s.start,
                        "end": s.end,
                        "text": s.text,
                        "avg_logprob": s.avg_logprob,
                        "no_speech_prob": s.no_speech_prob,
                    }
                    for s in segments
                ]
                transcript = "".join(s["text"] for s in saved).strip()
                if not transcript:
                    raise ProviderFailure("Речь не обнаружена. Запишите сообщение ещё раз")
                return {
                    "status_code": 200,
                    "request_id": None,
                    "body": json.dumps(
                        {
                            "text": transcript,
                            "engine": "faster-whisper",
                            "model": request["model"],
                            "revision": request["revision"],
                            "language": info.language,
                            "duration": info.duration,
                            "segments": saved,
                            "elapsed_seconds": time.monotonic() - started,
                        },
                        ensure_ascii=False,
                    ),
                }
        except ProviderFailure:
            raise
        except Exception as exc:
            # Do not expose filesystem details, decoded voice or runtime secrets.
            raise ProviderFailure(
                "Локальное распознавание Whisper завершилось ошибкой: " + type(exc).__name__
            ) from exc
