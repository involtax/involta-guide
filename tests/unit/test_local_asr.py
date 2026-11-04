import json
from types import SimpleNamespace

import pytest

from involta_voice.asr import MODELS, VAD, LocalAsr, model_path, options
from involta_voice.config import Settings
from involta_voice.provider import ProviderFailure, VoiceProvider


def settings(tmp_path):
    return Settings(
        _env_file=None,
        database_url="postgresql://test:test@localhost/test",
        service_secret="x" * 40,
        asr_models_dir=tmp_path,
    )


def test_voice_provider_rejects_analysis_without_a_cloud_client(tmp_path):
    provider = VoiceProvider(settings(tmp_path))
    with pytest.raises(ProviderFailure, match="отключён"):
        provider.call("analysis", {}, b"private-audio", "audio/wav")
    assert not hasattr(provider, "analysis")


@pytest.mark.parametrize("engine", ["gigaam", "parakeet"])
def test_asr_same_protocol_and_empty_speech_is_not_invented(tmp_path, monkeypatch, engine):
    import numpy as np

    from involta_voice import asr

    config = settings(tmp_path).model_copy(update={"transcription_model": engine})
    request = options(config)
    path = model_path(config)
    vad = config.asr_models_dir / "vad" / VAD["revision"]
    for directory in (path, vad):
        directory.mkdir(parents=True)
        (directory / "READY.json").write_text("{}")
    local = LocalAsr(config)
    monkeypatch.setattr(asr, "decode", lambda audio: np.zeros(16000, dtype=np.float32))

    class Model:
        speech = True

        def recognize(self, audio, **kwargs):
            assert kwargs["sample_rate"] == 16000
            return [SimpleNamespace(start=0, end=1, text=" Было вкусно.")] if self.speech else []

    local._model = Model()
    local._signature = (engine, request["revision"], config.asr_cpu_threads)
    result = json.loads(local.call(request, b"fixture")["body"])
    assert result["text"] == "Было вкусно."
    assert result["revision"] == MODELS[engine]["revision"]
    assert result["pcm_sha256"] and result["audio_sha256"]
    local._model.speech = False
    with pytest.raises(ProviderFailure, match="Речь не обнаружена"):
        local.call(request, b"fixture")


def test_model_is_prepared_explicitly_never_downloaded_during_upload(tmp_path):
    local = LocalAsr(settings(tmp_path))
    with pytest.raises(ProviderFailure, match="prepare-model"):
        local.call(options(local.settings), b"fixture")


@pytest.mark.parametrize(
    "extension,codec",
    [("wav", "pcm_s16le"), ("webm", "libopus"), ("ogg", "libopus"), ("m4a", "aac")],
)
def test_real_browser_container_decoding_without_model_or_network(tmp_path, extension, codec):
    import io
    import subprocess
    import wave

    from involta_voice.asr import decode

    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\0\0" * 16000)
    target = tmp_path / ("sample." + extension)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "wav", "-i", "pipe:0", "-c:a", codec, str(target)],
        input=stream.getvalue(),
        capture_output=True,
        check=True,
    )
    decoded = decode(target.read_bytes())
    assert 1.9 <= len(decoded) / 16000 <= 2.2
