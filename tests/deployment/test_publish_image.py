import importlib.util
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

SPEC = importlib.util.spec_from_file_location(
    "publish_image", Path(__file__).parents[2] / "deploy/publish-image.py"
)
assert SPEC and SPEC.loader
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)
IMAGE = "ghcr.io/involtax/involta-guide"
SHA = "a" * 40
ID = "sha256:" + "1" * 64
DIGEST = "sha256:" + "2" * 64


@pytest.fixture
def local(monkeypatch):
    inspected = {
        "Id": ID,
        "Config": {"Labels": {"org.opencontainers.image.revision": SHA}},
        "Os": "linux",
        "Architecture": "amd64",
    }
    monkeypatch.setattr(publisher, "docker", lambda *args: json.dumps([inspected]))
    return inspected


@pytest.mark.parametrize(
    "field,value", [("Id", DIGEST), ("Os", "windows"), ("Architecture", "arm64"), ("Config", {})]
)
def test_rejects_wrong_loaded_image(local, monkeypatch, field, value):
    local[field] = value
    remote = Mock()
    monkeypatch.setattr(publisher, "remote", remote)
    with pytest.raises(RuntimeError, match="Loaded image"):
        publisher.publish(IMAGE, SHA, ID)
    remote.assert_not_called()


def test_existing_identical_image_is_reused(local, monkeypatch):
    monkeypatch.setattr(publisher, "remote", lambda *args: (DIGEST, ID))
    push = Mock()
    monkeypatch.setattr(publisher.subprocess, "run", push)
    assert publisher.publish(IMAGE, SHA, ID) == DIGEST
    push.assert_not_called()


def test_existing_different_image_cannot_be_overwritten(local, monkeypatch):
    monkeypatch.setattr(publisher, "remote", lambda *args: (DIGEST, DIGEST))
    push = Mock()
    monkeypatch.setattr(publisher.subprocess, "run", push)
    with pytest.raises(RuntimeError, match="overwrite"):
        publisher.publish(IMAGE, SHA, ID)
    push.assert_not_called()


@pytest.mark.parametrize(
    "published,success", [((DIGEST, ID), True), ((DIGEST, DIGEST), False), (None, False)]
)
def test_push_checks_registry_identity(local, monkeypatch, published, success):
    monkeypatch.setattr(publisher, "remote", Mock(side_effect=[None, published]))
    push = Mock()
    monkeypatch.setattr(publisher.subprocess, "run", push)
    if success:
        assert publisher.publish(IMAGE, SHA, ID) == DIGEST
    else:
        with pytest.raises(RuntimeError, match="Published image config"):
            publisher.publish(IMAGE, SHA, ID)
    push.assert_called_once_with(["docker", "push", IMAGE + ":" + SHA], check=True, timeout=600)


@pytest.mark.parametrize(
    "error,missing",
    [
        (IMAGE + ":" + SHA + ": not found", True),
        ("manifest unknown", True),
        ("unauthorized", False),
        ("network timeout", False),
    ],
)
def test_registry_error_is_not_treated_as_absent_tag(monkeypatch, error, missing):
    monkeypatch.setattr(
        publisher.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 1, "", error),
    )
    if missing:
        assert publisher.remote(IMAGE, SHA) is None
    else:
        with pytest.raises(RuntimeError, match="Registry inspection failed"):
            publisher.remote(IMAGE, SHA)


def test_remote_reads_platform_manifest_by_digest(monkeypatch):
    monkeypatch.setattr(
        publisher.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, DIGEST, ""),
    )
    platform_digest = "sha256:" + "3" * 64
    docker = Mock(
        side_effect=[
            json.dumps(
                {
                    "manifests": [
                        {
                            "platform": {"os": "linux", "architecture": "amd64"},
                            "digest": platform_digest,
                        }
                    ]
                }
            ),
            json.dumps({"config": {"digest": ID}}),
        ]
    )
    monkeypatch.setattr(publisher, "docker", docker)
    assert publisher.remote(IMAGE, SHA) == (DIGEST, ID)
    assert docker.call_args.args == (
        "buildx",
        "imagetools",
        "inspect",
        IMAGE + "@" + platform_digest,
        "--raw",
    )
