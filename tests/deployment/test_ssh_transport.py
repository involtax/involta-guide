"""Exercise actual SSH wrapper with fake clients; never connects to a server."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FAKE = """import json,os,sys
from pathlib import Path
args=sys.argv[1:]
with open(os.environ['SSH_TEST_LOG'],'a') as out:
    out.write(json.dumps([Path(sys.argv[0]).name,args])+'\\n')
if Path(sys.argv[0]).name=='ssh':
    command=args[-1]
    if 'mkdir' in command and os.getenv('SSH_TEST_FAIL'):
        sys.exit(255)
    if 'id -u' in command:
        print('root')
    if 'remote-release.sh' in command or 'bootstrap-production.py' in command or 'sync-admin-password.py' in command:
        sys.stdin.read()
"""


@pytest.mark.parametrize(
    "mode",
    [
        "deploy",
        "rollback",
        "bootstrap",
        "bootstrap-rollback",
        "inspect",
        "admin-password",
        "voice-prepare",
        "voice-activate",
        "voice-backfill",
        "voice-report",
    ],
)
@pytest.mark.parametrize("pin", [None, "", "-", "example.test ssh-ed25519 test-public-key"])
def test_host_pin_policy_applies_to_every_connection(tmp_path, mode, pin):
    clients = tmp_path / "bin"
    clients.mkdir()
    for name in ("ssh", "scp"):
        path = clients / name
        path.write_text("#!" + sys.executable + "\n" + FAKE)
        path.chmod(0o700)
    log = tmp_path / "calls.jsonl"
    env = {
        **os.environ,
        "PATH": str(clients) + ":" + os.environ["PATH"],
        "SSH_TEST_LOG": str(log),
        "DEPLOY_SSH_HOST": "example.test",
        "DEPLOY_SSH_USER": "root",
        "DEPLOY_SSH_KEY": "test-only-private-key",
        "TARGET_SHA": "a" * 40,
        "IMAGE_REF": "ghcr.io/involtax/involta-guide@sha256:" + "1" * 64,
        "GHCR_TOKEN": "test-only-token",
        "GITHUB_ACTOR": "test-actor",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
        "SYNC_ADMIN_PASSWORD": "test-only-new-admin-password",
    }
    env.pop("DEPLOY_SSH_KNOWN_HOSTS", None)
    if pin is not None:
        env["DEPLOY_SSH_KNOWN_HOSTS"] = pin
    if mode in ("inspect", "admin-password"):
        for field in ("TARGET_SHA", "IMAGE_REF", "GHCR_TOKEN", "GITHUB_ACTOR"):
            env.pop(field, None)
    result = subprocess.run(
        ["bash", "deploy/ssh-release.sh", mode],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    strict = bool(pin and pin != "-")
    for _, args in calls:
        assert ("StrictHostKeyChecking=yes" if strict else "StrictHostKeyChecking=no") in args
        assert "GlobalKnownHostsFile=/dev/null" in args
        if not strict:
            assert "UserKnownHostsFile=/dev/null" in args
        assert not Path(args[args.index("-i") + 1]).exists()
    if mode == "inspect":
        assert all("mkdir" not in args[-1] and "rm -rf" not in args[-1] for _, args in calls)
    assert "test-only-private-key" not in result.stdout + result.stderr
    assert "test-only-token" not in result.stdout + result.stderr
    assert "test-only-new-admin-password" not in result.stdout + result.stderr
    assert ("enabled" if strict else "temporarily disabled") in result.stderr
