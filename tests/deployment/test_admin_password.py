import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from dotenv import dotenv_values


def module():
    path = Path(__file__).resolve().parents[2] / "deploy/sync-admin-password.py"
    spec = importlib.util.spec_from_file_location("admin_password_sync", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.mark.parametrize("fail_health", [False, True])
def test_atomic_rotation_preserves_other_secrets_and_rolls_back(
    tmp_path, monkeypatch, capsys, fail_health
):
    script = module()
    monkeypatch.setattr(script, "ROOT", tmp_path)
    monkeypatch.setattr(script, "LOCK", tmp_path / "global.lock")
    monkeypatch.setattr(script.os, "geteuid", lambda: 0)
    original = "OPENAI_API_KEY='test-preserve-key'\nALEX_SECRET_BASE64='dGVzdA=='\nADMIN_PASSWORD='old-password'\nADMIN_PASSWORD_HASH=''\n"
    (tmp_path / ".env.production").write_text(original)
    (tmp_path / "current-release").write_text(
        "a" * 40 + " ghcr.io/involtax/involta-guide@sha256:" + "b" * 64 + " 123-1\n"
    )
    (tmp_path / "deploy.env").write_text("AUDIO_VOLUME=involta-guide-audio\nNETWORK_MODE=host\n")
    password = "new-'пароль\n$arbitrary-value"
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"password": password, "run_id": "123"}))
    )
    failed = False
    commands = []

    def fake_run(args, *, data=None, env=None, timeout=300):
        nonlocal failed
        commands.append(args)
        if "exec" in args:
            program = args[-1]
            if "parse_stream" in program:
                return subprocess.run(
                    [sys.executable, "-c", program],
                    input=data,
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout
            values = dotenv_values(tmp_path / ".env.production", interpolate=False)
            assert values["OPENAI_API_KEY"] == "test-preserve-key"
            assert values["ADMIN_PASSWORD"] == ""
            assert PasswordHasher().verify(values["ADMIN_PASSWORD_HASH"], password)
            return "verified"
        if "inspect" in args:
            if "{{json .Config.Labels}}" in args:
                return json.dumps(
                    {
                        "com.involta.guide.owner": "production",
                        "com.docker.compose.project": "involta-guide-production",
                    }
                )
            if fail_health and not failed:
                failed = True
                return "unhealthy"
            return "healthy"
        if "ps" in args:
            return "own-container-id"
        return ""

    monkeypatch.setattr(script, "run", fake_run)
    if fail_health:
        with pytest.raises(RuntimeError, match="rolled back"):
            script.main()
        assert (tmp_path / ".env.production").read_text() == original
    else:
        script.main()
        values = dotenv_values(tmp_path / ".env.production", interpolate=False)
        assert values["ADMIN_PASSWORD"] == ""
        assert PasswordHasher().verify(values["ADMIN_PASSWORD_HASH"], password)
        assert (tmp_path / ".env.production").stat().st_mode & 0o777 == 0o600
    assert all(password not in str(args) for args in commands)
    assert not any("down" in args for args in commands)
    output = capsys.readouterr()
    assert password not in output.out + output.err
    assert "test-preserve-key" not in output.out + output.err


def test_empty_password_stops_before_any_server_change(monkeypatch):
    script = module()
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"password":""}'))
    with pytest.raises(RuntimeError, match="empty; server unchanged"):
        script.main()
