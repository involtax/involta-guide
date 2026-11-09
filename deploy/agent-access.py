"""Acquire API credentials through existing GitHub authorization, without manual SSH."""

import base64
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

REPOSITORY = "involtax/involta-guide"
WORKFLOW = "agent-api-access.yml"


def command(args, *, data=None, timeout=60):
    result = subprocess.run(args, input=data, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("Credential access failed; credentials were not printed")
    return result.stdout


def acquire(destination: Path) -> None:
    request_id = str(uuid4())
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory() as folder:
        folder = Path(folder)
        private, certificate = folder / "private.pem", folder / "recipient.pem"
        command(
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
                "/CN=involta-agent",
                "-keyout",
                str(private),
                "-out",
                str(certificate),
            ]
        )
        command(
            ["gh", "workflow", "run", WORKFLOW, "--repo", REPOSITORY, "--ref", "main", "--json"],
            data=json.dumps(
                {"request_id": request_id, "certificate": certificate.read_text()}
            ).encode(),
        )
        deadline = time.monotonic() + 900
        run_id = None
        print("Waiting for protected GitHub credential delivery…", flush=True)
        while time.monotonic() < deadline:
            if run_id is None:
                runs = json.loads(
                    command(
                        [
                            "gh",
                            "run",
                            "list",
                            "--repo",
                            REPOSITORY,
                            "--workflow",
                            WORKFLOW,
                            "--event",
                            "workflow_dispatch",
                            "--limit",
                            "30",
                            "--json",
                            "databaseId,displayTitle",
                        ]
                    )
                )
                run_id = next(
                    (
                        r["databaseId"]
                        for r in runs
                        if r["displayTitle"] == "Agent API access / " + request_id
                    ),
                    None,
                )
                if run_id is not None:
                    print(f"https://github.com/{REPOSITORY}/actions/runs/{run_id}", flush=True)
            if run_id is not None:
                run = json.loads(
                    command(
                        [
                            "gh",
                            "run",
                            "view",
                            str(run_id),
                            "--repo",
                            REPOSITORY,
                            "--json",
                            "status,conclusion",
                        ]
                    )
                )
                if run["status"] == "completed":
                    if run["conclusion"] != "success":
                        raise RuntimeError(
                            "Protected credential workflow failed; inspect its safe log"
                        )
                    break
            time.sleep(3)
        else:
            raise RuntimeError("Credential delivery timed out; no local credential changed")
        artifact = folder / "artifact"
        command(
            [
                "gh",
                "run",
                "download",
                str(run_id),
                "--repo",
                REPOSITORY,
                "--name",
                "agent-key-" + request_id,
                "--dir",
                str(artifact),
            ]
        )
        envelope = folder / "envelope.der"
        envelope.write_bytes(
            base64.b64decode((artifact / "credentials.cms.b64").read_bytes(), validate=False)
        )
        payload = command(
            [
                "openssl",
                "cms",
                "-decrypt",
                "-binary",
                "-inform",
                "DER",
                "-in",
                str(envelope),
                "-recip",
                str(certificate),
                "-inkey",
                str(private),
            ]
        )
        credentials = json.loads(payload)
        if (
            set(credentials) != {"base_url", "token"}
            or credentials["base_url"] != "https://involta.ru/guide"
            or not re.fullmatch(r"[A-Za-z0-9_+/=-]{32,256}", credentials["token"])
        ):
            raise RuntimeError("Invalid credential response; existing credentials preserved")
        temporary = destination.with_name(destination.name + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(credentials, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    print("API credentials saved privately: " + str(destination))


if __name__ == "__main__":
    os.umask(0o077)
    try:
        acquire(
            Path(sys.argv[1])
            if len(sys.argv) > 1
            else Path(__file__).resolve().parents[1] / ".env.agent-api"
        )
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError):
        sys.exit("Agent API access failed; no credential was printed. Check workflow status.")
