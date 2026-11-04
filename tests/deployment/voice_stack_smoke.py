"""Isolated host-Compose smoke. Requires disposable TEST_DATABASE_URL and a built VOICE_TEST_IMAGE.
Never use a production database. Uses generated fake credentials; performs no provider calls.
Ports 33080/13100 must be free. Keeps only generated env files for diagnosis.
"""

import io
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
import uuid
import wave
from pathlib import Path

import httpx
import psycopg

root = Path(tempfile.mkdtemp(prefix="voice-review-stack-"))
schema = "voice_smoke_" + uuid.uuid4().hex
secret = secrets.token_hex(24)
env = {
    "DEPLOY_ROOT": str(root),
    "IMAGE_REF": os.environ.get("VOICE_TEST_IMAGE", "involta-guide:voice-review"),
    "RELEASE_SHA": "a" * 40,
    "AUDIO_VOLUME": "voice-review-" + uuid.uuid4().hex,
    "GUIDE_HTTP_PORT": "33080",
    "OPENAI_API_KEY": "fake-never-called",
    "VOICE_SERVICE_SECRET": secret,
}
(root / ".env.production").write_text(
    "\n".join(
        f"{k}={v}"
        for k, v in {
            **env,
            "DATABASE_URL": os.environ["TEST_DATABASE_URL"],
            "DATABASE_SCHEMA": schema,
            "SESSION_SECRET": secret,
            "ADMIN_PASSWORD": "review-smoke-password",
            "VOICE_ENABLED": "true",
            "VOICE_URL": "http://127.0.0.1:13100",
            "SITE_URL": "http://127.0.0.1:33080/guide",
            "TEMPORARY_PUBLIC_FILL_ENABLED": "false",
            "NO_PROXY": "127.0.0.1,localhost",
            "COOKIE_SECURE": "false",
        }.items()
    )
    + "\n"
)
(root / ".env.voice").write_text(
    f"VOICE_DATABASE_SCHEMA={schema}_voice\nVOICE_FAKE_PROVIDER=true\nVOICE_MIN_FREE_BYTES=0\nNO_PROXY=127.0.0.1,localhost\n"
)
(root / "override.json").write_text(
    json.dumps(
        {
            "services": {
                "web": {"container_name": "involta-guide-review-" + uuid.uuid4().hex},
                "voice-prepare-model": {
                    "command": ["python", "-c", "print('fake-provider: model load not required')"]
                },
            }
        }
    )
)
for p in [root / ".env.production", root / ".env.voice"]:
    p.chmod(0o600)
cmd = ["docker", "--host=unix:///var/run/docker.sock"]
environ = {
    k: v
    for k, v in os.environ.items()
    if k
    not in ["DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"]
}
compose = cmd + [
    "compose",
    "-p",
    "voice-review-" + uuid.uuid4().hex,
    "--env-file",
    str(root / ".env.production"),
    "-f",
    "deploy/compose.host.yaml",
    "-f",
    "deploy/compose.voice.host.yaml",
    "-f",
    str(root / "override.json"),
]


def run(args):
    return subprocess.run(args, env=environ, check=True, stdout=subprocess.DEVNULL)


with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as c:
    c.execute("CREATE SCHEMA " + schema)
run(cmd + ["volume", "create", env["AUDIO_VOLUME"]])
try:
    run(compose + ["run", "--rm", "--no-deps", "web", "alembic", "upgrade", "head"])
    run(compose + ["run", "--rm", "--no-deps", "web", "guide", "seed"])
    run(compose + ["up", "-d", "--no-build", "--wait", "--wait-timeout", "90"])
    # Nested Docker may use vfs: release the completed one-off root filesystems
    # before testing audio storage. No running services or persistent volumes change.
    run(compose + ["rm", "--force", "voice-migrate", "voice-prepare-model"])
    with httpx.Client(
        base_url="http://127.0.0.1:33080", trust_env=False, timeout=10, follow_redirects=True
    ) as c:
        for _ in range(40):
            try:
                if c.get("/guide/health/ready").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        identity = c.post(
            "/guide/voice/api/v1/identity",
            params={"location": "ivanovo"},
            headers={"origin": "http://127.0.0.1:33080"},
        )
        assert identity.status_code == 200, identity.text
        epoch = identity.json()["content_epoch"]
        buf = io.BytesIO()
        with wave.open(buf, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b"\0\0" * 48000)
        r = c.post(
            "/guide/voice/api/v1/submissions",
            headers={"origin": "http://127.0.0.1:33080", "Content-Type": "audio/wav"},
            params={
                "location": "ivanovo",
                "content_epoch": epoch,
                "idempotency_key": str(uuid.uuid4()),
            },
            content=buf.getvalue(),
        )
        assert r.status_code == 202, r.text
        ref = r.json()["reference"]
        for _ in range(40):
            rows = c.get("/guide/voice/api/v1/reviews?location=ivanovo").json()["rows"]
            status = next((r for r in rows if r["reference"] == ref), {})
            if status.get("transcript"):
                break
            time.sleep(1)
        assert status.get("transcript"), status
        assert status["transcript"] in c.get("/guide/ivanovo").text
        assert c.get(status["log_url"]).status_code == 200
        with httpx.Client(
            base_url="http://127.0.0.1:13100",
            headers={"authorization": "Bearer " + secret},
            trust_env=False,
            timeout=10,
        ) as internal:
            for _ in range(40):
                response = internal.post("/internal/v1/admin/recognitions", json=[ref])
                response.raise_for_status()
                recognitions = response.json()[ref]
                if len(recognitions) == 6 and all(r["state"] == "completed" for r in recognitions):
                    break
                time.sleep(1)
            else:
                raise AssertionError("The worker did not complete all six recognition variants")
            assert {r["variant"] for r in recognitions} == {
                "onnx",
                "sherpa_plain",
                "sherpa_hotwords",
            }
            assert all("dictionary" in r for r in recognitions)
        stopping = time.monotonic()
        run(compose + ["stop", "voice"])
        elapsed = time.monotonic() - stopping
        print(f"Combined voice stopped in {elapsed:.2f}s")
        assert elapsed < 15
        assert c.get("/guide/ivanovo").status_code == 200
        assert c.get("/guide/voice/api/v1/reviews?location=ivanovo").json()["rows"][0]["transcript"]
        login = c.get("/guide/admin").text
        csrf = re.search(r'name="_csrf" value="([^"]+)"', login).group(1)
        assert (
            c.post(
                "/guide/admin/login",
                headers={"origin": "http://127.0.0.1:33080"},
                data={"_csrf": csrf, "password": "review-smoke-password"},
            ).status_code
            == 200
        )
        listing = c.get("/guide/admin/reviews/ivanovo").text
        csrf = re.search(r'name="_csrf" value="([^"]+)"', listing).group(1)
        deletion = re.search(r'action="(/guide/admin/reviews/ivanovo/\d+/delete)"', listing).group(
            1
        )
        assert (
            c.post(
                deletion, headers={"origin": "http://127.0.0.1:33080"}, data={"_csrf": csrf}
            ).status_code
            == 200
        )
        assert c.get("/guide/voice/api/v1/reviews?location=ivanovo").json()["rows"] == []
        print(
            "PASS: production host Compose, migrations, worker, consumer, public reviews, public logs, admin deletion and offline HTML. Fake provider only."
        )
except Exception:
    subprocess.run(compose + ["logs", "--no-color", "--tail", "35"], env=environ)
    raise
finally:
    subprocess.run(compose + ["down", "--volumes"], env=environ, stdout=subprocess.DEVNULL)
    subprocess.run(
        cmd + ["volume", "rm", env["AUDIO_VOLUME"]], env=environ, stdout=subprocess.DEVNULL
    )
    with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS " + schema + "_voice CASCADE")
        c.execute("DROP SCHEMA " + schema + " CASCADE")
