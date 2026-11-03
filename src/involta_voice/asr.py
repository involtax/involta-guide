"""Pinned, offline GigaAM/Parakeet inference with the same decoding/VAD protocol."""

import gc
import hashlib
import io
import json
import threading
import time
from pathlib import Path
from typing import Any

from .config import Settings
from .provider import ProviderFailure

MODELS: dict[str, dict[str, Any]] = {
    "gigaam": {
        "name": "GigaAM v3 E2E RNN-T",
        "loader": "gigaam-v3-e2e-rnnt",
        "repository": "istupakov/gigaam-v3-onnx",
        "revision": "322c3b29492673eb7d0b434bfa9dfb8653e34d02",
        "license": "MIT",
        "files": [
            "config.json",
            "v3_e2e_rnnt_encoder.int8.onnx",
            "v3_e2e_rnnt_decoder.int8.onnx",
            "v3_e2e_rnnt_joint.int8.onnx",
            "v3_e2e_rnnt_vocab.txt",
            "README.md",
            "LICENSE.txt",
        ],
    },
    "parakeet": {
        "name": "Parakeet TDT 0.6B v3",
        "loader": "nemo-parakeet-tdt-0.6b-v3",
        "repository": "istupakov/parakeet-tdt-0.6b-v3-onnx",
        "revision": "8f23f0c03c8761650bdb5b40aaf3e40d2c15f1ce",
        "license": "CC-BY-4.0",
        "files": [
            "config.json",
            "encoder-model.int8.onnx",
            "decoder_joint-model.int8.onnx",
            "vocab.txt",
            "README.md",
        ],
    },
}
VAD = {
    "repository": "istupakov/silero-vad-onnx",
    "revision": "b3e3ee3cce4c11ceb63b1a0b229d916069c1ddf6",
    "files": ["config.json", "silero_vad.onnx", "README.md", "LICENSE.txt"],
}


def options(settings: Settings, engine: str | None = None) -> dict[str, Any]:
    engine = engine or settings.transcription_model
    return {
        "engine": "onnx-asr",
        "model": engine,
        "revision": MODELS[engine]["revision"],
        "quantization": "int8",
        "cpu_threads": settings.asr_cpu_threads,
        "sample_rate": 16000,
        "vad_revision": VAD["revision"],
        "max_speech_duration_s": 20,
        "min_silence_duration_ms": 500,
        "preprocessing": "av-mono-16k-v1",
    }


def model_path(settings: Settings, request: dict[str, Any] | None = None) -> Path:
    request = request or options(settings)
    return settings.asr_models_dir / request["model"] / request["revision"]


def prepare(settings: Settings) -> Path:
    from huggingface_hub import snapshot_download

    engines = list(MODELS) if settings.asr_beta_enabled else [settings.transcription_model]
    for name, definition in [(e, MODELS[e]) for e in engines] + [("vad", VAD)]:
        target = settings.asr_models_dir / name / definition["revision"]
        target.mkdir(parents=True, exist_ok=True)
        snapshot_download(
            definition["repository"],
            revision=definition["revision"],
            local_dir=target,
            allow_patterns=definition["files"],
        )
        if not all((target / filename).is_file() for filename in definition["files"]):
            raise RuntimeError("Неполная загрузка ASR модели")
        (target / "READY.json").write_text(
            json.dumps({"repository": definition["repository"], "revision": definition["revision"]})
        )
    return settings.asr_models_dir


def decode(audio: bytes) -> Any:
    import av
    import numpy as np

    frames: list[Any] = []
    with av.open(io.BytesIO(audio), mode="r") as container:
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=16000)
        for frame in container.decode(audio=0):
            frames.extend(f.to_ndarray().reshape(-1) for f in resampler.resample(frame))
        frames.extend(f.to_ndarray().reshape(-1) for f in resampler.resample(None))
    if not frames:
        raise ProviderFailure("Аудиопоток пуст")
    return np.concatenate(frames).astype(np.float32, copy=False)


class LocalAsr:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._model: Any = None
        self._vad: Any = None
        self._signature: Any = None
        self._lock = threading.RLock()

    def warmup(self, request: dict[str, Any] | None = None) -> None:
        request = request or options(self.settings)
        if (
            request.get("engine") != "onnx-asr"
            or request.get("model") not in MODELS
            or request.get("revision") != MODELS.get(str(request.get("model")), {}).get("revision")
        ):
            raise ProviderFailure("Нужна новая обработка с GigaAM/Parakeet")
        root = model_path(self.settings, request)
        vad = self.settings.asr_models_dir / "vad" / request["vad_revision"]
        if not (root / "READY.json").is_file() or not (vad / "READY.json").is_file():
            raise ProviderFailure("ASR модели не подготовлены: выполните voice prepare-model")
        with self._lock:
            signature = (request["model"], request["revision"], request["cpu_threads"])
            if self._model is None or self._signature != signature:
                self._model = None
                self._signature = None
                gc.collect()
                import onnxruntime as ort

                ort.disable_telemetry_events()
                import onnx_asr

                session = ort.SessionOptions()
                session.intra_op_num_threads = request["cpu_threads"]
                session.inter_op_num_threads = 1
                if self._vad is None:
                    self._vad = onnx_asr.load_vad(
                        "silero", path=vad, sess_options=session, providers=["CPUExecutionProvider"]
                    )
                self._model = onnx_asr.load_model(
                    MODELS[request["model"]]["loader"],
                    path=root,
                    quantization="int8",
                    sess_options=session,
                    providers=["CPUExecutionProvider"],
                ).with_vad(
                    self._vad,
                    batch_size=1,
                    max_speech_duration_s=request["max_speech_duration_s"],
                    min_silence_duration_ms=request["min_silence_duration_ms"],
                )
                self._signature = signature

    def call(self, request: dict[str, Any], audio: bytes) -> dict[str, Any]:
        started = time.monotonic()
        try:
            with self._lock:
                self.warmup(request)
                waveform = decode(audio)
                loaded = time.monotonic()
                parts = list(self._model.recognize(waveform, sample_rate=16000))
                text = " ".join(p.text.strip() for p in parts if p.text.strip()).strip()
                if not text or not any(c.isalnum() for c in text):
                    raise ProviderFailure("Речь не обнаружена. Запишите сообщение ещё раз")
                body = {
                    "text": text,
                    "engine": request["model"],
                    "model": MODELS[request["model"]]["name"],
                    "revision": request["revision"],
                    "audio_sha256": hashlib.sha256(audio).hexdigest(),
                    "pcm_sha256": hashlib.sha256(waveform.tobytes()).hexdigest(),
                    "duration": len(waveform) / 16000,
                    "elapsed_seconds": time.monotonic() - started,
                    "inference_seconds": time.monotonic() - loaded,
                    "segments": [{"start": p.start, "end": p.end, "text": p.text} for p in parts],
                    "runtime": "onnx-asr",
                    "settings": request,
                }
                return {
                    "status_code": 200,
                    "request_id": None,
                    "body": json.dumps(body, ensure_ascii=False),
                }
        except ProviderFailure:
            raise
        except Exception as exc:
            raise ProviderFailure(
                "Локальное ASR завершилось ошибкой: " + type(exc).__name__
            ) from exc
