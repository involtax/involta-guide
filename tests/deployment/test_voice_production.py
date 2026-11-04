import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("mode", ["host", "bridge"])
def test_voice_production_composition(tmp_path, mode):
    if not shutil.which("docker"):
        pytest.skip("Docker Compose required")
    for filename in [".env.production", ".env.voice"]:
        (tmp_path / filename).touch()
    env = {
        **os.environ,
        "DEPLOY_ROOT": str(tmp_path),
        "IMAGE_REF": "ghcr.io/involtax/involta-guide@sha256:" + "1" * 64,
        "RELEASE_SHA": "a" * 40,
        "VOICE_POSTGRES_IMAGE": "postgres@sha256:" + "2" * 64,
        "AUDIO_VOLUME": "test-audio",
        "VOICE_POSTGRES_PASSWORD": "test-only",
        "VOICE_SERVICE_SECRET": "test-only-" + "x" * 32,
        "OPENAI_API_KEY": "test-key-no-api-calls",
    }
    base = "deploy/compose.host.yaml" if mode == "host" else "deploy/compose.production.yaml"
    overlay = "deploy/compose.voice.host.yaml" if mode == "host" else "deploy/compose.voice.yaml"
    result = subprocess.run(
        ["docker", "compose", "-f", base, "-f", overlay, "config", "--format", "json"],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    services = json.loads(result.stdout)["services"]
    assert {
        "voice",
        "voice-worker",
        "voice-migrate",
        "web",
        "worker",
    } <= services.keys()
    for name in ["voice", "voice-worker"]:
        assert services[name]["healthcheck"]
        assert "build" not in services[name]
        assert "@sha256:" in services[name]["image"]
    assert services["voice"]["environment"]["OPENAI_API_KEY"] == "test-key-no-api-calls"
    assert "VOICE_OPENAI_API_KEY" not in services["voice"]["environment"]
    assert not services["voice"].get("ports")
    assert "voice-consumer" not in services and "voice-db" not in services
    assert (
        services["voice"]["command"][1] == "involta_guide.infrastructure.voice.runtime:create_app"
    )
    assert not services["voice"]["environment"].get("DATABASE_URL")
