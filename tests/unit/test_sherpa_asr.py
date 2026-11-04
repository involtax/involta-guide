import hashlib
import json
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from involta_voice.asr import VAD
from involta_voice.config import Settings
from involta_voice.provider import ProviderFailure
from involta_voice.sherpa import LocalSherpa, model_path, options
from involta_voice.sherpa_tokenizer import VOCAB_FILE, tokenizer, vocabulary
from involta_voice.variants import SHERPA_PROFILES


def settings(tmp_path):
    return Settings(
        _env_file=None,
        database_url="postgresql://test:test@localhost/test",
        service_secret="x" * 40,
        asr_models_dir=tmp_path,
    )


@pytest.mark.parametrize("engine", ["gigaam"])
def test_sherpa_comparison_changes_only_dictionary_not_search_or_preprocessing(tmp_path, engine):
    config = settings(tmp_path)
    names = ["Алхимист", "Огонёк", "Алхимист", "  Сыроварня  "]
    plain = options(config, engine, "sherpa_plain", names)
    biased = options(config, engine, "sherpa_hotwords", names)
    assert {k: v for k, v in plain.items() if k not in ("variant", "dictionary")} == {
        k: v for k, v in biased.items() if k not in ("variant", "dictionary")
    }
    assert biased["dictionary"] == [
        "Алхимист",
        "Огонёк",
        "Сыроварня",
        "алхимист",
        "огонёк",
        "сыроварня",
    ]
    assert plain["dictionary"] == []
    assert plain["decoding_method"] == "modified_beam_search"
    names.append("Не должно попасть в запрос")
    assert len(biased["dictionary"]) == 6


def test_missing_models_are_reported_before_import_or_any_download(tmp_path):
    config = settings(tmp_path)
    with pytest.raises(ProviderFailure, match="prepare-model"):
        LocalSherpa(config).call(options(config, "gigaam", "sherpa_plain", []), b"fixture")


@pytest.mark.parametrize("variant", list(SHERPA_PROFILES))
def test_sherpa_uses_cpu_dictionary_file_same_vad_segments_and_saved_settings(
    tmp_path, monkeypatch, variant
):
    from involta_voice import sherpa

    config = settings(tmp_path)
    engine = "gigaam"
    request = options(config, engine, variant, ["Алхимист", "Бар / Кухня"], location_name="Плёс")
    for path in (model_path(config, engine), config.asr_models_dir / "vad" / VAD["revision"]):
        path.mkdir(parents=True)
        (path / "READY.json").write_text("{}")
    root = model_path(config, engine)
    sp = tokenizer()
    (root / "tokens.txt").write_text(
        "".join(f"{sp.id_to_piece(i)} {i}\n" for i in range(sp.vocab_size()))
        + f"<blk> {sp.vocab_size()}\n"
    )
    (root / VOCAB_FILE).write_bytes(vocabulary(root / "tokens.txt"))
    waveform = np.zeros(16000, dtype=np.float32)
    monkeypatch.setattr(sherpa, "decode", lambda audio: waveform)
    captured = {}

    class Vad:
        silent = False

        def segment_batch(self, audio, lengths, sample_rate, **kwargs):
            assert sample_rate == 16000 and kwargs["max_speech_duration_s"] == 20
            assert kwargs["min_silence_duration_ms"] == 500
            yield iter([] if self.silent else [(0, 8000), (8000, 16000)])

    vad = Vad()

    class Stream:
        result = SimpleNamespace(text="Ресторан Алхимист.")

        def set_option(self, name, value):
            assert name == "length_reward"
            assert float(value) == 0

        def accept_waveform(self, sample_rate, audio):
            assert sample_rate == 16000 and len(audio) == 8000

    class Model:
        def create_stream(self):
            return Stream()

        def decode_stream(self, stream):
            pass

    def factory(**kwargs):
        from pathlib import Path

        assert kwargs["provider"] == "cpu" and kwargs["model_type"] == "nemo_transducer"
        assert kwargs["decoding_method"] == "modified_beam_search"
        assert kwargs["max_active_paths"] == request["max_active_paths"]
        assert kwargs["hotwords_score"] == request["hotwords_score"]
        if request["dictionary"]:
            assert Path(kwargs["hotwords_file"]).read_text().splitlines() == request["dictionary"]
        else:
            assert kwargs["hotwords_file"] == ""
        captured.update(kwargs)
        return Model()

    monkeypatch.setitem(
        sys.modules, "onnx_asr", SimpleNamespace(load_vad=lambda *args, **kwargs: vad)
    )
    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        SimpleNamespace(SessionOptions=SimpleNamespace, disable_telemetry_events=lambda: None),
    )
    monkeypatch.setitem(
        sys.modules,
        "sherpa_onnx",
        SimpleNamespace(
            OfflineRecognizer=SimpleNamespace(from_transducer=factory),
            git_sha1="upstream-reported-revision",
        ),
    )
    local = LocalSherpa(config)
    result = json.loads(local.call(request, b"private fixture")["body"])
    assert result["settings"] == request
    assert result["pcm_sha256"] == hashlib.sha256(waveform.tobytes()).hexdigest()
    assert result["audio_sha256"] == hashlib.sha256(b"private fixture").hexdigest()
    assert result["segments"] == [
        {"start": 0, "end": 0.5, "text": "Ресторан Алхимист."},
        {"start": 0.5, "end": 1, "text": "Ресторан Алхимист."},
    ]
    from pathlib import Path

    if captured["hotwords_file"]:
        assert not Path(captured["hotwords_file"]).exists()
    vad.silent = True
    with pytest.raises(ProviderFailure, match="Речь не обнаружена"):
        local.call(request, b"fixture")
    local.close()
    assert local._model is None


@pytest.mark.parametrize("contents", [None, b"corrupt"])
def test_native_vocabulary_missing_or_corrupt_fails_before_runtime_import(tmp_path, contents):
    config = settings(tmp_path)
    root = model_path(config, "gigaam")
    for path in (root, config.asr_models_dir / "vad" / VAD["revision"]):
        path.mkdir(parents=True)
        (path / "READY.json").write_text("{}")
    if contents is not None:
        (root / VOCAB_FILE).write_bytes(contents)
    with pytest.raises(ProviderFailure, match="Родной словарь GigaAM не подготовлен"):
        LocalSherpa(config).warmup(options(config, "gigaam", "sherpa_hotwords", ["Плёс"]))
