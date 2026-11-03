"""Manual isolated host-Compose smoke. Requires disposable TEST_DATABASE_URL and a built VOICE_TEST_IMAGE.
Never use a production database. Uses generated fake credentials; performs no provider calls.
Ports 33080/13100/55439 must be free. Keeps only generated env files for diagnosis.
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
    "VOICE_POSTGRES_IMAGE": "postgres:17-alpine",
    "VOICE_POSTGRES_PASSWORD": secret,
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
    f"VOICE_DATABASE_URL=postgresql://voice:{secret}@127.0.0.1:55439/voice\nVOICE_FAKE_PROVIDER=true\nVOICE_MIN_FREE_BYTES=0\nNO_PROXY=127.0.0.1,localhost\n"
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
    run(compose + ["up", "-d", "--no-build"])
    with httpx.Client(base_url="http://127.0.0.1:33080", trust_env=False, timeout=10) as c:
        for _ in range(40):
            try:
                if c.get("/guide/health/ready").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        c.post("/guide/voice/api/v1/identity", headers={"origin": "http://127.0.0.1:33080"})
        buf = io.BytesIO()
        with wave.open(buf, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b"\0\0" * 48000)
        r = c.post(
            "/guide/voice/api/v1/submissions",
            headers={"origin": "http://127.0.0.1:33080"},
            data={"location": "ivanovo", "idempotency_key": str(uuid.uuid4())},
            files={"audio": ("x.wav", buf.getvalue(), "audio/wav")},
        )
        assert r.status_code == 202, r.text
        ref = r.json()["reference"]
        for _ in range(40):
            status = c.get("/guide/voice/api/v1/submissions/" + ref).json()
            if status.get("transcript") and status.get("editorial_state") == "pending":
                break
            time.sleep(1)
        assert status.get("transcript"), status
        login = c.get("/guide/admin").text
        csrf = re.search(r'name="_csrf" value="([^"]+)"', login).group(1)
        r = c.post(
            "/guide/admin/login",
            headers={"origin": "http://127.0.0.1:33080"},
            data={"_csrf": csrf, "password": "review-smoke-password"},
        )
        assert r.status_code in [200, 303], r.text
        page = c.get("/guide/admin/voice/ivanovo/1").text
        csrf = re.search(r'name="_csrf" value="([^"]+)"', page).group(1)
        revision = re.search(r'name="review_revision" value="([^"]+)"', page).group(1)
        r = c.post(
            "/guide/admin/voice/decision/" + ref,
            headers={"origin": "http://127.0.0.1:33080"},
            data={
                "_csrf": csrf,
                "idempotency_key": str(uuid.uuid4()),
                "review_revision": revision,
                "processing_run": "1",
                "action": "approve",
            },
        )
        assert r.status_code in [200, 303], r.text
        assert status.get("transcript"), status in c.get("/guide/ivanovo").text
        run(compose + ["stop", "voice-api"])
        assert c.get("/guide/ivanovo").status_code == 200
        assert c.get("/guide/voice/api/v1/submissions/" + ref).json()["transcript"]
        print(
            "PASS: production host Compose, migrations, worker, consumer, review approval and offline history/HTML. Fake provider only."
        )
finally:
    subprocess.run(compose + ["down", "--volumes"], env=environ, stdout=subprocess.DEVNULL)
    subprocess.run(
        cmd + ["volume", "rm", env["AUDIO_VOLUME"]], env=environ, stdout=subprocess.DEVNULL
    )
    with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as c:
        c.execute("DROP SCHEMA " + schema + " CASCADE")
