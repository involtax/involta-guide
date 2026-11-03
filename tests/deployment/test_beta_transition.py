"""Coordinated deployment failure boundaries; no production services are touched."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "beta_transition", Path(__file__).resolve().parents[2] / "deploy/beta-transition.py"
)
beta = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(beta)


class Operation:
    def __init__(self, fail=None, fail_restore=False):
        self.events = []
        self.fail = fail
        self.fail_restore = fail_restore

    def __getattr__(self, name):
        def action():
            self.events.append(name)
            if name == self.fail or (name == "restore" and self.fail_restore):
                raise RuntimeError(name)

        return action


def test_success_stops_writers_before_consistent_backup_and_commits_only_after_health():
    operation = Operation()
    beta.execute(operation)
    assert operation.events == [
        "preflight",
        "stop",
        "backup",
        "migrate",
        "start",
        "verify",
        "commit",
    ]


@pytest.mark.parametrize(
    "failure", ["preflight", "stop", "backup", "migrate", "start", "verify", "commit"]
)
def test_failure_never_restarts_old_image_before_restoring_both_databases(failure):
    operation = Operation(failure)
    with pytest.raises(RuntimeError, match=failure):
        beta.execute(operation)
    if failure == "preflight":
        assert operation.events == ["preflight"]
    elif failure in ("stop", "backup"):
        assert operation.events[-1] == "restart_old" and "restore" not in operation.events
    else:
        assert operation.events[-2:] == ["restore", "restart_old"]


def test_failed_restore_keeps_writers_stopped():
    operation = Operation("migrate", fail_restore=True)
    with pytest.raises(RuntimeError, match="restore"):
        beta.execute(operation)
    assert operation.events[-1] == "restore" and "restart_old" not in operation.events


def request():
    return {
        "sha": "a" * 40,
        "image": "ghcr.io/involtax/involta-guide@sha256:" + "1" * 64,
        "run_id": "123-1",
        "actor": "test-actor",
        "token": "private-registry-token",
    }


def test_compose_commands_pin_their_own_image_even_after_another_command_is_created(monkeypatch):
    operation = beta.Transition(request())
    operation.deployment = {"NETWORK_MODE": "host"}
    old = operation.compose("b" * 40, "old-image")
    new = operation.compose(operation.sha, operation.image)
    assert old[:3] == ["env", "RELEASE_SHA=" + "b" * 40, "IMAGE_REF=old-image"]
    assert new[:3] == ["env", "RELEASE_SHA=" + operation.sha, "IMAGE_REF=" + operation.image]


def test_public_error_keeps_useful_stderr_and_redacts_credentials(monkeypatch):
    operation = beta.Transition(request())
    operation.redactions += ["private-db-password"]
    monkeypatch.setattr(
        beta.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a, 1, b"", b"database connection refused private-db-password private-registry-token"
        ),
    )
    with pytest.raises(RuntimeError, match="database connection refused") as error:
        operation.run(["test"])
    assert "private-db-password" not in str(error.value)
    assert "private-registry-token" not in str(error.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("sha", "main"),
        ("image", "ghcr.io/involtax/involta-guide:latest"),
        ("run_id", "../../foreign"),
    ],
)
def test_unsafe_or_mutable_target_rejected_before_io(field, value):
    data = request()
    data[field] = value
    with pytest.raises(AssertionError):
        beta.Transition(data)
