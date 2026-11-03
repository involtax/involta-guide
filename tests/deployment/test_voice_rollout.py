import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def operation(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("voice_rollout", ROOT / "deploy/voice-rollout.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = tmp_path
    (tmp_path / "current-release").write_text(
        "a" * 40 + " ghcr.io/example@sha256:" + "1" * 64 + " 1-1"
    )
    (tmp_path / "deploy.env").write_text("NETWORK_MODE=host\nAUDIO_VOLUME=existing-audio\n")
    (tmp_path / ".env.production").write_text(
        "OPENAI_API_KEY=test-only-key\nVOICE_ENABLED=false\nUNCHANGED=retain\n"
    )
    monkeypatch.setattr(
        module,
        "open",
        lambda path, mode: open(
            tmp_path / "global.lock" if str(path).startswith("/run/lock/") else path, mode
        ),
        raising=False,
    )
    monkeypatch.setattr(
        module,
        "inspect",
        lambda name: {
            "Config": {
                "Image": "ghcr.io/example@sha256:" + "1" * 64,
                "Labels": {module.OWNER: "production"},
            }
        },
    )
    monkeypatch.setattr(module.fcntl, "flock", lambda *args: None)
    # Avoid changing the test runner's umask.
    monkeypatch.setattr(module.os, "umask", lambda value: None)
    return module


def test_preflight_health_failure_never_enables_intake(operation, monkeypatch):
    monkeypatch.setattr(operation.sys, "argv", ["voice-rollout", "activate", "a" * 40])

    def fail(*args):
        raise RuntimeError("worker unavailable")

    monkeypatch.setattr(operation, "healthy", fail)
    with pytest.raises(RuntimeError, match="worker unavailable"):
        operation.main()
    assert operation.config(operation.ROOT / ".env.production")["VOICE_ENABLED"] == "false"


def test_atomic_configuration_retains_existing_credentials_and_permissions(operation):
    path = operation.ROOT / ".env.voice"
    path.write_text(
        "VOICE_DATABASE_URL=existing-private-url\nVOICE_POSTGRES_PASSWORD=existing-private-password\n"
    )
    operation.write_config(path, {"VOICE_ASR_BETA_ENABLED": "true"})
    value = operation.config(path)
    assert value["VOICE_DATABASE_URL"] == "existing-private-url"
    assert value["VOICE_POSTGRES_PASSWORD"] == "existing-private-password"
    assert value["VOICE_ASR_BETA_ENABLED"] == "true"
    assert path.stat().st_mode & 0o777 == 0o600
    assert not path.with_name(path.name + ".voice-tmp").exists()


def test_command_diagnostics_are_private_and_not_in_exception(operation, monkeypatch):
    monkeypatch.setattr(
        operation.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stderr="private-DSN", stdout="private-key"
        ),
    )
    with pytest.raises(RuntimeError) as error:
        operation.run(["example"])
    assert "private-DSN" not in str(error.value) and "private-key" not in str(error.value)
    private = operation.ROOT / "voice-operation-error.log"
    assert private.stat().st_mode & 0o777 == 0o600
    assert "private-DSN" in private.read_text()
