import pytest

from involta_voice.config import Settings
from involta_voice.provider import OpenAIProvider, ProviderFailure


def test_shared_openai_key_alias_and_missing_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-shared-key-for-config-test")
    settings = Settings(
        _env_file=None,
        database_url="postgresql://test:test@localhost/test",
        service_secret="x" * 40,
    )
    assert settings.openai_api_key.get_secret_value() == "fake-shared-key-for-config-test"
    monkeypatch.delenv("OPENAI_API_KEY")
    empty = Settings(
        _env_file=None,
        database_url="postgresql://test:test@localhost/test",
        service_secret="x" * 40,
    )
    assert not empty.openai_api_key.get_secret_value()
    provider = OpenAIProvider(empty)
    try:
        with pytest.raises(ProviderFailure, match="OPENAI_API_KEY"):
            provider.call("analysis", {}, b"", "audio/wav")
    finally:
        provider.close()
