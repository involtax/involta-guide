"""Provision the server token; export only encrypted credentials to an authorized runner."""

import base64
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

TOKEN = re.compile(r"[A-Za-z0-9_+/=-]{32,256}")
KEY_LINE = re.compile(r"^\s*(?:export\s+)?AGENT_API_TOKEN\s*=\s*(.*?)\s*$", re.MULTILINE)


def server_token(path: Path, *, create: bool) -> str:
    content = path.read_text()
    matches = KEY_LINE.findall(content)
    if matches:
        if len(matches) != 1:
            raise ValueError("Duplicate API key configuration")
        token = matches[0]
        if token.startswith(("'", '"')):
            if len(token) < 2 or token[-1] != token[0]:
                raise ValueError("Invalid quoted API key")
            token = token[1:-1]
        if not TOKEN.fullmatch(token):
            raise ValueError("Invalid existing API key; it was not replaced")
        return token
    if not create:
        raise ValueError("Deploy the API-enabled release first")
    token = secrets.token_hex(32)
    temporary = path.with_name(path.name + ".agent-api-tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content.rstrip("\n") + "\nAGENT_API_TOKEN=" + token + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return token


def encrypted_credentials(token: str, certificate: str) -> str:
    if len(certificate) > 8192 or not certificate.startswith("-----BEGIN CERTIFICATE-----\n"):
        raise ValueError("A recipient certificate is required")
    with TemporaryDirectory() as folder:
        recipient = Path(folder) / "recipient.pem"
        recipient.write_text(certificate)
        payload = json.dumps({"base_url": "https://involta.ru/guide", "token": token}).encode()
        result = subprocess.run(
            [
                "openssl",
                "cms",
                "-encrypt",
                "-binary",
                "-aes-256-cbc",
                "-outform",
                "DER",
                "-recip",
                str(recipient),
                "-keyopt",
                "rsa_padding_mode:oaep",
                "-keyopt",
                "rsa_oaep_md:sha256",
            ],
            input=payload,
            capture_output=True,
            timeout=15,
        )
        if result.returncode:
            raise ValueError("Cannot encrypt credentials for the recipient")
        return base64.b64encode(result.stdout).decode("ascii")


def main() -> None:
    os.umask(0o077)
    action = sys.argv[1]
    path = Path(sys.argv[2]) if action == "ensure" else Path("/opt/involta-guide/.env.production")
    if action == "ensure":
        server_token(path, create=True)
        print("Agent API key provisioned; existing credentials preserved")
    elif action == "export":
        request = json.loads(sys.stdin.read(16385))
        if (
            not isinstance(request, dict)
            or set(request) != {"certificate"}
            or not isinstance(request["certificate"], str)
        ):
            raise ValueError("Invalid credential request")
        token = server_token(path, create=False)
        print(encrypted_credentials(token, request["certificate"]))
    else:
        raise ValueError("Unknown operation")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError):
        sys.exit("Agent API credential operation failed; no secret was printed")
