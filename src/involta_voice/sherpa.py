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
from .audio_chunks import fragmented_speech, pack_speech
from .config import Settings
from .provider import ProviderFailure
from .sherpa_tokenizer import ENCODING, TOKENIZER_SHA256, VOCAB_FILE, VOCAB_SHA256, vocabulary
from .variants import COMMON_RECOGNITION_NAMES, SHERPA_PROFILES, dictionary_names

# Includes the NeMo TDT beam-search/length-reward fix, upstream PR #4084.
RUNTIME_REVISION = "3898de1380029e96a3b0a3df9e21e3f5ee5eb7fb"
VAD_TAIL_CONTEXT = "short-tail-context-v1"
SPEECH_CHUNKING = "fragmented-pauses-v1"
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


def options(
    settings: Settings, engine: str, variant: str, names: list[str], *, location_name: str = ""
) -> dict[str, Any]:
    if variant not in SHERPA_PROFILES:
        raise ValueError("Неизвестный вариант Sherpa")
    score, paths, source = SHERPA_PROFILES[variant]
    dictionary = [location_name, *names, *COMMON_RECOGNITION_NAMES] if source == "catalogue" else []
    if location_name and source == "catalogue":
        from .text import name_forms

        # Spoken city names are inflected; freeze real forms before ASR as well.
        dictionary.extend(sorted(name_forms(location_name, city=True)))
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
        "max_active_paths": paths,
        "hotwords_score": score,
        "length_reward": 0.0,
        "dictionary": dictionary_names([v for name in dictionary for v in (name, name.lower())]),
        "dictionary_encoding": ENCODING,
        "tokenizer_sha256": TOKENIZER_SHA256,
        "bpe_vocab_sha256": VOCAB_SHA256,
        "vad_revision": VAD["revision"],
        "max_speech_duration_s": 20,
        "min_silence_duration_ms": 500,
        "vad_tail_context": VAD_TAIL_CONTEXT,
        "speech_chunking": SPEECH_CHUNKING,
        "max_pause_ms": 300,
        "preprocessing": "av-mono-16k-v1",
    }


def model_path(settings: Settings, engine: str) -> Path:
    return settings.asr_models_dir / "sherpa" / engine / str(MODELS[engine]["revision"])


def join_short_tail(
    segments: list[tuple[int, int]], sample_rate: int, max_duration_s: float
) -> list[tuple[int, int]]:
    """Give an isolated short final sound its preceding speech context, without cutting it."""
    if len(segments) < 2:
        return segments
    previous_start, previous_end = segments[-2]
    start, end = segments[-1]
    if (
        0 < (end - start) * 1000 < 400 * sample_rate
        and 0 <= start - previous_end <= 3 * sample_rate
        and end - previous_start <= min(max_duration_s, 20) * sample_rate
    ):
        return [*segments[:-2], (previous_start, end)]
    return segments


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
        # Versioned filename preserves the exact vocabulary of old queued requests.
        prepared = target / (VOCAB_FILE + ".tmp")
        prepared.write_bytes(vocabulary(target / "tokens.txt"))
        prepared.replace(target / VOCAB_FILE)
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
        if type(request.get("force_speech_packing", False)) is not bool:
            raise ProviderFailure("Неизвестный режим сборки речевых блоков")
        if request.get("speech_chunking") not in (None, SPEECH_CHUNKING):
            raise ProviderFailure("Неизвестная версия сборки речевых блоков")
        if request.get("speech_chunking") is not None and (
            type(request.get("max_pause_ms")) is not int
            or request["max_pause_ms"] not in (200, 300, 500)
        ):
            raise ProviderFailure("Неизвестная длина паузы речевого блока")
        if request.get("vad_tail_context") not in (None, VAD_TAIL_CONTEXT):
            raise ProviderFailure("Неизвестная версия обработки хвоста VAD")
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
        encoding = request.get("dictionary_encoding", "tokens-longest-match-v1")
        if encoding == ENCODING:
            if (
                request.get("tokenizer_sha256") != TOKENIZER_SHA256
                or request.get("bpe_vocab_sha256") != VOCAB_SHA256
            ):
                raise ProviderFailure("Неизвестная ревизия токенизатора GigaAM")
            bpe_vocab = root / VOCAB_FILE
            if (
                not bpe_vocab.is_file()
                or hashlib.sha256(bpe_vocab.read_bytes()).hexdigest() != VOCAB_SHA256
            ):
                raise ProviderFailure(
                    "Родной словарь GigaAM не подготовлен: выполните voice prepare-model"
                )
        elif encoding == "tokens-longest-match-v1":
            bpe_vocab = root / "bpe.vocab"
            if not bpe_vocab.is_file():
                raise ProviderFailure("Исходный словарь исторического задания недоступен")
        else:
            raise ProviderFailure("Неизвестное кодирование словаря GigaAM")
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
                bpe_vocab=str(bpe_vocab),
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
                vad_segments = list(
                    next(
                        self._vad.segment_batch(
                            waveform[None, :],
                            np.asarray([len(waveform)], dtype=np.int64),
                            16000,
                            max_speech_duration_s=request["max_speech_duration_s"],
                            min_silence_duration_ms=request["min_silence_duration_ms"],
                        )
                    )
                )
                segments = (
                    join_short_tail(
                        vad_segments, request["sample_rate"], request["max_speech_duration_s"]
                    )
                    if request.get("vad_tail_context") == VAD_TAIL_CONTEXT
                    else vad_segments
                )
                packing = request.get("speech_chunking") == SPEECH_CHUNKING and (
                    request.get("force_speech_packing", False)
                    or fragmented_speech(vad_segments, request["sample_rate"])
                )
                chunks = (
                    pack_speech(
                        vad_segments,
                        request["sample_rate"],
                        min(request["max_speech_duration_s"], 20),
                        request["max_pause_ms"],
                    )
                    if packing
                    else [[segment] for segment in segments]
                )
                parts: list[dict[str, Any]] = []
                input_samples = 0
                for ranges in chunks:
                    samples = (
                        np.concatenate([waveform[start:end] for start, end in ranges])
                        if len(ranges) > 1
                        else waveform[ranges[0][0] : ranges[0][1]]
                    )
                    stream = self._model.create_stream()
                    stream.set_option("length_reward", str(request["length_reward"]))
                    stream.accept_waveform(16000, samples)
                    self._model.decode_stream(stream)
                    part = {
                        "start": ranges[0][0] / 16000,
                        "end": ranges[-1][1] / 16000,
                        "text": stream.result.text,
                    }
                    if packing:
                        offset = 0
                        mapping = []
                        for start, end in ranges:
                            mapping.append(
                                {
                                    "source_start": start / 16000,
                                    "source_end": end / 16000,
                                    "input_start": offset / 16000,
                                    "input_end": (offset + end - start) / 16000,
                                }
                            )
                            offset += end - start
                        part.update(
                            source_ranges=mapping,
                            input_duration=len(samples) / 16000,
                            pcm_sha256=hashlib.sha256(samples.tobytes()).hexdigest(),
                        )
                    input_samples += len(samples)
                    parts.append(part)
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
                    "asr_input_seconds": input_samples / 16000,
                    "elapsed_seconds": time.monotonic() - started,
                    "inference_seconds": time.monotonic() - loaded,
                    "segments": parts,
                    "vad_segments": [
                        {"start": start / 16000, "end": end / 16000} for start, end in vad_segments
                    ],
                    "vad_tail_context_applied": not packing and segments != vad_segments,
                    "speech_chunking_applied": packing,
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
