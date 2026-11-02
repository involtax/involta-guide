"""Rotate only Guide admin credentials; password arrives on stdin, never in argv."""

import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/opt/involta-guide")
LOCK = Path("/run/lock/involta-guide-production.lock")
DOCKER = ["docker", "--host=unix:///var/run/docker.sock"]


def run(args, *, data=None, env=None, timeout=300):
    result = subprocess.run(
        args, input=data, text=True, capture_output=True, timeout=timeout, env=env
    )
    if result.returncode:
        raise RuntimeError(
            "Credential operation failed (command output withheld to protect secrets)"
        )
    return result.stdout


def replace_credentials(contents, password):
    # Use dependencies already present in the running application, not host packages.
    program = """
import io,json,sys
from argon2 import PasswordHasher
from dotenv.parser import parse_stream
request=json.load(sys.stdin)
bindings=list(parse_stream(io.StringIO(request['content'])))
if any(b.error for b in bindings):
    sys.exit(2)
remaining=''.join(b.original.string for b in bindings if b.key not in ('ADMIN_PASSWORD','ADMIN_PASSWORD_HASH'))
hashed=PasswordHasher().hash(request['password'])
print(json.dumps({'content':remaining.rstrip('\\n')+"\\nADMIN_PASSWORD=''\\nADMIN_PASSWORD_HASH='"+hashed+"'\\n"}))
"""
    output = run(
        [*DOCKER, "exec", "-i", "involta-guide", "python", "-c", program],
        data=json.dumps({"content": contents, "password": password}),
    )
    return json.loads(output)["content"]


def write_env(contents):
    temporary = ROOT / ".env.production.admin-update"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as out:
            out.write(contents)
            out.flush()
            os.fsync(out.fileno())
        temporary.replace(ROOT / ".env.production")
    finally:
        temporary.unlink(missing_ok=True)


def main():
    request = json.load(sys.stdin)
    password = request.get("password", "")
    if not isinstance(password, str) or not password:
        raise RuntimeError("GitHub prod ADMIN_PASSWORD is empty; server unchanged")
    if os.geteuid() != 0:
        raise RuntimeError("Root or passwordless sudo is required; server unchanged")
    with LOCK.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = (ROOT / "current-release").read_text().split()
        if (
            len(state) != 3
            or not re.fullmatch(r"[0-9a-f]{40}", state[0])
            or not re.fullmatch(r"ghcr\.io/[a-z0-9._/-]+@sha256:[0-9a-f]{64}", state[1])
        ):
            raise RuntimeError("Invalid current release; server unchanged")
        options = dict(
            line.split("=", 1)
            for line in (ROOT / "deploy.env").read_text().splitlines()
            if line and not line.startswith("#")
        )
        allowed = {
            "AUDIO_VOLUME",
            "GUIDE_APPARMOR_PROFILE",
            "GUIDE_HTTP_PORT",
            "GUIDE_BIND_ADDRESS",
            "BASE_PATH",
            "NGINX_NETWORK",
            "NETWORK_MODE",
            "POSTGRES_IMAGE",
        }
        if not set(options) <= allowed:
            raise RuntimeError("Unknown runtime options; server unchanged")
        env = {k: v for k, v in os.environ.items() if not k.startswith("DOCKER_")}
        env.update(options, DEPLOY_ROOT=str(ROOT), RELEASE_SHA=state[0], IMAGE_REF=state[1])
        compose = [
            *DOCKER,
            "compose",
            "--project-name",
            "involta-guide-production",
            "--env-file",
            str(ROOT / ".env.production"),
            "--file",
            str(ROOT / "releases" / state[0] / "compose.yaml"),
        ]
        for name in ("involta-guide", "involta-guide-production-worker-1"):
            metadata = json.loads(
                run([*DOCKER, "inspect", "--format", "{{json .Config.Labels}}", name])
            )
            if (
                metadata.get("com.involta.guide.owner") != "production"
                or metadata.get("com.docker.compose.project") != "involta-guide-production"
            ):
                raise RuntimeError("Container ownership mismatch; server unchanged")
        run([*compose, "config", "--quiet"], env=env)
        original = (ROOT / ".env.production").read_text()
        updated = replace_credentials(original, password)

        def healthy(service):
            container = run([*compose, "ps", "--quiet", service], env=env).strip()
            for _ in range(60):
                status = run(
                    [*DOCKER, "inspect", "--format", "{{.State.Health.Status}}", container]
                ).strip()
                if status == "healthy":
                    return
                if status == "unhealthy":
                    break
                time.sleep(2)
            raise RuntimeError("Guide healthcheck failed during credential rotation")

        try:
            write_env(updated)
            run([*compose, "stop", "--timeout", "240", "worker"], env=env)
            run(
                [*compose, "up", "-d", "--no-deps", "--no-build", "--force-recreate", "web"],
                env=env,
            )
            healthy("web")
            verify = """
import json,sys
from argon2 import PasswordHasher
from sqlalchemy import text
from uuid import uuid4
from involta_guide.config import Settings
from involta_guide.infrastructure.persistence.unit_of_work import create_database
request=json.load(sys.stdin)
settings=Settings()
assert not settings.admin_password.get_secret_value()
assert PasswordHasher().verify(settings.admin_password_hash.get_secret_value(),request['password'])
engine=create_database(settings)
with engine.begin() as conn:
    conn.execute(text('UPDATE admin_sessions SET revoked=true WHERE NOT revoked'))
    conn.execute(text("INSERT INTO audit(id,action,target,detail) VALUES(:id,'admin_password_rotated','admin',:detail)"),{'id':str(uuid4()),'detail':request.get('run_id','')})
engine.dispose()
print('New admin password verified; previous admin sessions revoked')
"""
            run(
                [*DOCKER, "exec", "-i", "involta-guide", "python", "-c", verify],
                data=json.dumps(request),
            )
            run(
                [*compose, "up", "-d", "--no-deps", "--no-build", "--force-recreate", "worker"],
                env=env,
            )
            healthy("worker")
        except Exception:
            print("Credential update failed; restoring previous credentials", flush=True)
            write_env(original)
            run(
                [
                    *compose,
                    "up",
                    "-d",
                    "--no-deps",
                    "--no-build",
                    "--force-recreate",
                    "web",
                    "worker",
                ],
                env=env,
            )
            healthy("web")
            healthy("worker")
            print("Previous credentials restored; Guide is healthy", flush=True)
            raise RuntimeError("Admin credential update rolled back") from None
        print(
            "Admin password updated and verified; old sessions revoked; web and worker healthy",
            flush=True,
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Never emit traceback/locals/subprocess stderr while handling credentials.
        print(
            str(exc)
            if isinstance(exc, RuntimeError)
            else "Credential operation stopped; sensitive diagnostics withheld",
            file=sys.stderr,
        )
        sys.exit(1)
