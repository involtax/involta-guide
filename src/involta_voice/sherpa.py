"""CPU-only, pinned Sherpa comparisons over the same audio and Silero segments."""

import gc
import hashlib
import json
import threading
import time
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from .asr import VAD, decode
from .config import Settings
from .provider import ProviderFailure
from .variants import dictionary_names

# Includes the NeMo TDT beam-search/length-reward fix, upstream PR #4084.
RUNTIME_REVISION = "3898de1380029e96a3b0a3df9e21e3f5ee5eb7fb"
MODELS: dict[str, dict[str, Any]] = {
    "gigaam": {
        "repository": "csukuangfj/sherpa-onnx-nemo-transducer-punct-giga-am-v3-russian-2025-12-16",
        "revision": "a6039be7cee829a9044a69ac0ebaf1c191217c97",
        "files": ["encoder.int8.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt", "LICENSE"],
        "decoder": "decoder.onnx",
        "joiner": "joiner.onnx",
        "feature_dim": 64,
        "license": "MIT",
    },
}


def options(settings: Settings, engine: str, variant: str, names: list[str]) -> dict[str, Any]:
    if variant not in ("sherpa_plain", "sherpa_hotwords"):
        raise ValueError("Неизвестный вариант Sherpa")
    return {
        "engine": "sherpa-onnx",
        "model": engine,
        "variant": variant,
        "revision": MODELS[engine]["revision"],
        "runtime_revision": RUNTIME_REVISION,
        "quantization": "encoder-int8",
        "cpu_threads": settings.asr_cpu_threads,
        "sample_rate": 16000,
        "feature_dim": MODELS[engine]["feature_dim"],
        "model_type": "nemo_transducer",
        "decoding_method": "modified_beam_search",
        "max_active_paths": 4,
        "hotwords_score": 1.5,
        "length_reward": 0.0,
        "dictionary": dictionary_names(names) if variant == "sherpa_hotwords" else [],
        "dictionary_encoding": "tokens-longest-match-v1",
        "vad_revision": VAD["revision"],
        "max_speech_duration_s": 20,
        "min_silence_duration_ms": 500,
        "preprocessing": "av-mono-16k-v1",
    }


def model_path(settings: Settings, engine: str) -> Path:
    return settings.asr_models_dir / "sherpa" / engine / str(MODELS[engine]["revision"])


def prepare(settings: Settings) -> None:
    from huggingface_hub import snapshot_download

    for engine, definition in MODELS.items():
        target = model_path(settings, engine)
        target.mkdir(parents=True, exist_ok=True)
        snapshot_download(
            str(definition["repository"]),
            revision=str(definition["revision"]),
            local_dir=target,
            allow_patterns=list(definition["files"]),
        )
        if not all((target / name).is_file() for name in definition["files"]):
            raise RuntimeError("Неполная загрузка модели Sherpa")
        # Upstream Sherpa's Parakeet hotwords example derives the BPE vocabulary
        # from tokens.txt in this way. This is an encoding for hints, not a new ASR vocabulary.
        pieces = [
            line.rsplit(" ", 1)[0] for line in (target / "tokens.txt").read_text().splitlines()
        ]
        (target / "bpe.vocab").write_text(
            "".join(piece + "\t0\n" for piece in pieces if not piece.startswith("<"))
        )
        (target / "READY.json").write_text(
            json.dumps({"repository": definition["repository"], "revision": definition["revision"]})
        )


class LocalSherpa:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._model: Any = None
        self._vad: Any = None
        self._signature: str | None = None
        self._runtime_reported_revision: str | None = None
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._model = None
            self._signature = None
            gc.collect()

    def warmup(self, request: dict[str, Any]) -> None:
        engine = str(request.get("model") or "")
        if (
            request.get("engine") != "sherpa-onnx"
            or engine not in MODELS
            or request.get("revision") != MODELS[engine]["revision"]
            or request.get("runtime_revision") != RUNTIME_REVISION
        ):
            raise ProviderFailure("Неизвестная ревизия модели/исполнителя Sherpa")
        root = model_path(self.settings, engine)
        vad = self.settings.asr_models_dir / "vad" / request["vad_revision"]
        if not (root / "READY.json").is_file() or not (vad / "READY.json").is_file():
            raise ProviderFailure("Модели Sherpa не подготовлены: выполните voice prepare-model")
        signature = json.dumps(request, sort_keys=True, ensure_ascii=False)
        if self._signature == signature and self._model is not None:
            return
        self.close()
        import onnx_asr
        import onnxruntime as ort
        import sherpa_onnx

        self._runtime_reported_revision = sherpa_onnx.git_sha1

        ort.disable_telemetry_events()
        if self._vad is None:
            session = ort.SessionOptions()
            session.intra_op_num_threads = request["cpu_threads"]
            session.inter_op_num_threads = 1
            self._vad = onnx_asr.load_vad(
                "silero", path=vad, sess_options=session, providers=["CPUExecutionProvider"]
            )
        definition = MODELS[engine]
        # File-based phrases preserve '/' inside restaurant names; the per-stream
        # string API treats it as a phrase separator. No dictionary file is retained.
        with NamedTemporaryFile(mode="w+", encoding="utf-8", suffix=".hotwords") as phrases:
            names = request["dictionary"]
            if any(word.startswith(":") for name in names for word in name.split()):
                raise ProviderFailure("Название в словаре содержит управляющий префикс Sherpa ':'")
            phrases.write("\n".join(names))
            phrases.flush()
            self._model = sherpa_onnx.OfflineRecognizer.from_transducer(
                encoder=str(root / "encoder.int8.onnx"),
                decoder=str(root / str(definition["decoder"])),
                joiner=str(root / str(definition["joiner"])),
                tokens=str(root / "tokens.txt"),
                num_threads=request["cpu_threads"],
                sample_rate=request["sample_rate"],
                feature_dim=request["feature_dim"],
                model_type=request["model_type"],
                decoding_method=request["decoding_method"],
                max_active_paths=request["max_active_paths"],
                modeling_unit="bpe",
                bpe_vocab=str(root / "bpe.vocab"),
                hotwords_file=phrases.name if names else "",
                hotwords_score=request["hotwords_score"],
                provider="cpu",
            )
        self._signature = signature

    def call(self, request: dict[str, Any], audio: bytes) -> dict[str, Any]:
        import numpy as np

        started = time.monotonic()
        try:
            with self._lock:
                self.warmup(request)
                waveform = decode(audio)
                loaded = time.monotonic()
                segments = next(
                    self._vad.segment_batch(
                        waveform[None, :],
                        np.asarray([len(waveform)], dtype=np.int64),
                        16000,
                        max_speech_duration_s=request["max_speech_duration_s"],
                        min_silence_duration_ms=request["min_silence_duration_ms"],
                    )
                )
                parts: list[dict[str, Any]] = []
                for start, end in segments:
                    stream = self._model.create_stream()
                    stream.set_option("length_reward", str(request["length_reward"]))
                    stream.accept_waveform(16000, waveform[start:end])
                    self._model.decode_stream(stream)
                    parts.append(
                        {"start": start / 16000, "end": end / 16000, "text": stream.result.text}
                    )
                text = " ".join(p["text"].strip() for p in parts if p["text"].strip())
                if not text or not any(c.isalnum() for c in text):
                    raise ProviderFailure("Речь не обнаружена")
                body = {
                    "text": text,
                    "engine": request["model"],
                    "runtime": "sherpa-onnx",
                    "revision": request["revision"],
                    "runtime_revision": RUNTIME_REVISION,
                    "runtime_reported_revision": self._runtime_reported_revision,
                    "audio_sha256": hashlib.sha256(audio).hexdigest(),
                    "pcm_sha256": hashlib.sha256(waveform.tobytes()).hexdigest(),
                    "duration": len(waveform) / 16000,
                    "elapsed_seconds": time.monotonic() - started,
                    "inference_seconds": time.monotonic() - loaded,
                    "segments": parts,
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
                "Sherpa завершилась ошибкой: " + type(exc).__name__ + ": " + str(exc)[:2000]
            ) from exc
