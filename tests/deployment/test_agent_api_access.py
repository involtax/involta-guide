"""No production calls: real encryption and mocked protected delivery exercise secret handling."""

import base64
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def module(filename):
    spec = importlib.util.spec_from_file_location(
        filename.replace("-", "_"), ROOT / "deploy" / filename
    )
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def recipient(folder, name="recipient"):
    key, cert = folder / (name + ".key"), folder / (name + ".pem")
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=test-agent",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        capture_output=True,
        check=True,
    )
    return key, cert


def test_provision_preserves_all_other_config_and_never_rotates(tmp_path):
    server = module("agent-api-key.py")
    path = tmp_path / ".env.production"
    original = "DATABASE_URL='postgresql://example'\nSESSION_SECRET='keep-this-value'\n"
    path.write_text(original)
    token = server.server_token(path, create=True)
    assert len(token) == 64
    assert path.read_text() == original + "AGENT_API_TOKEN=" + token + "\n"
    assert path.stat().st_mode & 0o777 == 0o600
    assert server.server_token(path, create=True) == token
    assert server.server_token(path, create=False) == token
    assert not list(tmp_path.glob("*.agent-api-tmp"))


@pytest.mark.parametrize(
    "value",
    [
        "AGENT_API_TOKEN=short\n",
        "AGENT_API_TOKEN=" + ("a" * 32) + "\nAGENT_API_TOKEN=" + ("b" * 32) + "\n",
        'AGENT_API_TOKEN="' + ("a" * 32) + "\n",
    ],
)
def test_invalid_existing_credentials_are_never_replaced(tmp_path, value):
    server = module("agent-api-key.py")
    path = tmp_path / ".env.production"
    path.write_text(value)
    with pytest.raises(ValueError):
        server.server_token(path, create=True)
    assert path.read_text() == value


def test_missing_credentials_cannot_be_exported_or_created_by_a_read(tmp_path):
    server = module("agent-api-key.py")
    path = tmp_path / ".env.production"
    path.write_text("OTHER=unchanged\n")
    with pytest.raises(ValueError):
        server.server_token(path, create=False)
    assert path.read_text() == "OTHER=unchanged\n"


def test_credential_envelope_requires_exact_recipient_and_contains_no_plaintext(tmp_path):
    server = module("agent-api-key.py")
    key, cert = recipient(tmp_path)
    otherkey, othercert = recipient(tmp_path, "other")
    token = "sensitive-" + "z" * 40
    encoded = server.encrypted_credentials(token, cert.read_text())
    raw = base64.b64decode(encoded)
    assert token not in encoded and token.encode() not in raw
    envelope = tmp_path / "envelope.der"
    envelope.write_bytes(raw)
    command = ["openssl", "cms", "-decrypt", "-binary", "-inform", "DER", "-in", str(envelope)]
    decrypted = subprocess.run(
        [*command, "-recip", str(cert), "-inkey", str(key)], capture_output=True, check=True
    )
    assert json.loads(decrypted.stdout) == {"base_url": "https://involta.ru/guide", "token": token}
    assert (
        subprocess.run(
            [*command, "-recip", str(othercert), "-inkey", str(otherkey)], capture_output=True
        ).returncode
        != 0
    )


@pytest.mark.parametrize(
    "certificate", ["not-a-certificate", "-----BEGIN CERTIFICATE-----\n" + "x" * 9000]
)
def test_invalid_certificate_does_not_return_a_token(certificate):
    server = module("agent-api-key.py")
    with pytest.raises(ValueError):
        server.encrypted_credentials("a" * 64, certificate)


def test_client_requests_only_public_certificate_and_saves_private_credentials(
    tmp_path, monkeypatch, capsys
):
    server = module("agent-api-key.py")
    client = module("agent-access.py")
    real = client.command
    token = "server-secret-" + "s" * 40
    ciphertext = None
    calls = []

    def command(args, *, data=None, timeout=60):
        nonlocal ciphertext
        calls.append(args)
        if args[0] != "gh":
            return real(args, data=data, timeout=timeout)
        if args[1:3] == ["workflow", "run"]:
            request = json.loads(data)
            assert set(request) == {"request_id", "certificate"}
            assert token not in data.decode() and "PRIVATE KEY" not in data.decode()
            request_id.append(request["request_id"])
            ciphertext = server.encrypted_credentials(token, request["certificate"])
            return b""
        if args[1:3] == ["run", "list"]:
            return json.dumps(
                [{"databaseId": 123, "displayTitle": "Agent API access / " + request_id[0]}]
            ).encode()
        if args[1:3] == ["run", "view"]:
            return b'{"status":"completed","conclusion":"success"}'
        if args[1:3] == ["run", "download"]:
            folder = Path(args[args.index("--dir") + 1])
            folder.mkdir()
            (folder / "credentials.cms.b64").write_text(ciphertext)
            return b""
        raise AssertionError(args)

    request_id = []
    monkeypatch.setattr(client, "command", command)
    target = tmp_path / ".env.agent-api"
    oldmask = os.umask(0o077)
    try:
        client.acquire(target)
    finally:
        os.umask(oldmask)
    assert json.loads(target.read_text())["token"] == token
    assert target.stat().st_mode & 0o777 == 0o600
    assert all(token not in str(args) for args in calls)
    captured = capsys.readouterr()
    assert token not in captured.out + captured.err
    assert not any("PRIVATE KEY" in str(args) for args in calls)


def test_workflow_uses_existing_protected_channel_and_only_encrypted_artifact(tmp_path):
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/agent-api-access.yml").read_text())
    job = workflow["jobs"]["deliver"]
    assert job["environment"] == "prod" and "refs/heads/main" in job["if"]
    steps = job["steps"]
    upload = next(
        step for step in steps if step.get("uses", "").startswith("actions/upload-artifact")
    )
    assert upload["with"]["path"] == "${{ runner.temp }}/credentials.cms.b64"
    assert upload["with"]["retention-days"] == 1
    validation = steps[1]["run"]
    _, cert = recipient(tmp_path)
    subprocess.run(
        ["bash", "-c", validation],
        env={
            **os.environ,
            "REQUEST_ID": "a38b6263-71ce-40e7-9996-06a897730bf5",
            "AGENT_RECIPIENT_CERTIFICATE": cert.read_text(),
        },
        check=True,
        capture_output=True,
    )
    assert "PRIVATE KEY" not in validation
    assert "secrets.AGENT_API_TOKEN" not in json.dumps(workflow)


@pytest.mark.parametrize("blank", ["", "''", '""', "   "])
def test_blank_api_key_never_reads_the_next_secret_and_can_be_provisioned(tmp_path, blank):
    server = module("agent-api-key.py")
    path = tmp_path / ".env.production"
    other_secret = "keep-other-secret-" + "x" * 40
    original = "AGENT_API_TOKEN=" + blank + "\nOPENAI_API_KEY=" + other_secret + "\n"
    path.write_text(original)
    with pytest.raises(ValueError):
        server.server_token(path, create=False)
    assert path.read_text() == original
    token = server.server_token(path, create=True)
    assert token != other_secret and len(token) == 64
    assert (
        path.read_text() == "AGENT_API_TOKEN=" + token + "\nOPENAI_API_KEY=" + other_secret + "\n"
    )
    assert server.server_token(path, create=False) == token
