import hashlib
import json
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from involta_voice.asr import VAD
from involta_voice.config import Settings
from involta_voice.provider import ProviderFailure
from involta_voice.sherpa import VAD_TAIL_CONTEXT, LocalSherpa, model_path, options
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
    biased = options(config, engine, "sherpa_hotwords_strong", names)
    assert biased["hotwords_score"] == 2.5 and biased["max_active_paths"] == 16
    with pytest.raises(ValueError):
        options(config, engine, "sherpa_plain", names)
    assert biased["dictionary"] == [
        "Алхимист",
        "Огонёк",
        "Сыроварня",
        "алхимист",
        "огонёк",
        "сыроварня",
    ]
    assert biased["decoding_method"] == "modified_beam_search"
    names.append("Не должно попасть в запрос")
    assert len(biased["dictionary"]) == 6


def test_missing_models_are_reported_before_import_or_any_download(tmp_path):
    config = settings(tmp_path)
    with pytest.raises(ProviderFailure, match="prepare-model"):
        LocalSherpa(config).call(
            options(config, "gigaam", "sherpa_hotwords_strong", []), b"fixture"
        )


@pytest.mark.parametrize("variant", list(SHERPA_PROFILES))
@pytest.mark.parametrize("tail_policy", [None, VAD_TAIL_CONTEXT])
def test_sherpa_uses_cpu_dictionary_file_same_vad_segments_and_saved_settings(
    tmp_path, monkeypatch, variant, tail_policy
):
    from involta_voice import sherpa

    config = settings(tmp_path)
    engine = "gigaam"
    request = options(config, engine, variant, ["Алхимист", "Бар / Кухня"], location_name="Плёс")
    if tail_policy is None:
        request.pop("vad_tail_context")
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
            yield iter([] if self.silent else [(0, 8000), (12000, 16000)])

    vad = Vad()
    accepted_lengths = []

    class Stream:
        result = SimpleNamespace(text="Ресторан Алхимист.")

        def set_option(self, name, value):
            assert name == "length_reward"
            assert float(value) == 0

        def accept_waveform(self, sample_rate, audio):
            assert sample_rate == 16000
            accepted_lengths.append(len(audio))

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
    assert result["vad_segments"] == [{"start": 0, "end": 0.5}, {"start": 0.75, "end": 1}]
    assert result["vad_tail_context_applied"] == (tail_policy is not None)
    assert result["segments"] == (
        [{"start": 0, "end": 1, "text": "Ресторан Алхимист."}]
        if tail_policy
        else [
            {"start": 0, "end": 0.5, "text": "Ресторан Алхимист."},
            {"start": 0.75, "end": 1, "text": "Ресторан Алхимист."},
        ]
    )
    assert accepted_lengths == ([16000] if tail_policy else [8000, 4000])
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
        LocalSherpa(config).warmup(options(config, "gigaam", "sherpa_hotwords_strong", ["Плёс"]))


@pytest.mark.parametrize(
    ("segments", "limit", "expected"),
    [
        ([], 20, []),
        ([(0, 3000)], 20, [(0, 3000)]),
        ([(32, 48608), (91680, 96736)], 20, [(32, 96736)]),
        ([(0, 16000), (16000, 22400)], 20, [(0, 16000), (16000, 22400)]),
        ([(0, 16000), (16000, 22399)], 20, [(0, 22399)]),
        ([(0, 16000), (64000, 67000)], 20, [(0, 67000)]),
        ([(0, 16000), (64001, 67000)], 20, [(0, 16000), (64001, 67000)]),
        ([(0, 316000), (316001, 320000)], 20, [(0, 320000)]),
        ([(0, 316000), (316002, 320001)], 20, [(0, 316000), (316002, 320001)]),
        ([(0, 16000), (16000, 19000)], 1, [(0, 16000), (16000, 19000)]),
        ([(0, 16000), (15000, 18000)], 20, [(0, 16000), (15000, 18000)]),
        (
            [(0, 16000), (32000, 35000), (48000, 64000)],
            20,
            [(0, 16000), (32000, 35000), (48000, 64000)],
        ),
        ([(0, 8000), (10000, 16000), (20000, 23000)], 20, [(0, 8000), (10000, 23000)]),
    ],
)
def test_short_tail_context_preserves_audio_and_respects_boundaries(segments, limit, expected):
    from involta_voice.sherpa import join_short_tail

    original = list(segments)
    actual = join_short_tail(segments, 16000, limit)
    assert actual == expected
    assert segments == original
    assert join_short_tail(actual, 16000, limit) == actual


def test_unknown_tail_policy_fails_before_loading_models(tmp_path):
    request = options(settings(tmp_path), "gigaam", "sherpa_hotwords_strong", [])
    request["vad_tail_context"] = "unknown-v9"
    with pytest.raises(ProviderFailure, match="Неизвестная версия"):
        LocalSherpa(settings(tmp_path)).call(request, b"not-an-audio")
