import json
from types import SimpleNamespace

import httpx
import pytest

from involta_voice.config import Settings
from involta_voice.provider import OpenAIProvider, ProviderFailure
from involta_voice.whisper import LocalWhisper, model_path, options


def settings(tmp_path):
    return Settings(
        _env_file=None,
        database_url="postgresql://test:test@localhost/test",
        service_secret="x" * 40,
        whisper_models_dir=tmp_path,
    )


def test_cloud_provider_refuses_audio_before_http(tmp_path):
    provider = OpenAIProvider(settings(tmp_path))
    provider.client.close()

    def deny(request):
        raise AssertionError("Audio must never leave the server")

    provider.client = httpx.Client(transport=httpx.MockTransport(deny))
    try:
        with pytest.raises(ProviderFailure, match="локальным Whisper"):
            provider.call("transcription", {}, b"private-audio", "audio/wav")
    finally:
        provider.close()


def test_whisper_is_offline_russian_vad_and_empty_speech_is_not_invented(tmp_path):
    config = settings(tmp_path)
    path = model_path(config)
    path.mkdir(parents=True)
    (path / "READY.json").write_text("{}")
    local = LocalWhisper(config)

    class Model:
        speech = True

        def transcribe(self, audio, **kwargs):
            assert kwargs["language"] == "ru" and kwargs["vad_filter"]
            assert not kwargs["condition_on_previous_text"]
            segment = SimpleNamespace(
                start=0, end=1, text=" Было вкусно.", avg_logprob=-0.1, no_speech_prob=0.01
            )
            return iter([segment] if self.speech else []), SimpleNamespace(
                language="ru", duration=2
            )

    local._model = Model()
    local._signature = (
        config.transcription_model,
        options(config)["revision"],
        "cpu",
        "int8",
        config.whisper_cpu_threads,
    )
    result = local.call(options(config), b"fixture")
    assert json.loads(result["body"])["text"] == "Было вкусно."
    local._model.speech = False
    with pytest.raises(ProviderFailure, match="Речь не обнаружена"):
        local.call(options(config), b"fixture")


def test_model_is_prepared_explicitly_never_downloaded_during_upload(tmp_path):
    local = LocalWhisper(settings(tmp_path))
    with pytest.raises(ProviderFailure, match="prepare-model"):
        local.call(options(local.settings), b"fixture")
