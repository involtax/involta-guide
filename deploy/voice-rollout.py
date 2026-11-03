"""Protected first Voice rollout and corpus evaluation. Credentials stay on the server."""

import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/opt/involta-guide")
DOCKER = ["docker", "--host=unix:///var/run/docker.sock"]
OWNER = "com.involta.guide.owner"


def run(args, timeout=300):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Raw stderr can contain DSNs; preserve privately, never send to Actions.
        private = ROOT / "voice-operation-error.log"
        private.write_text(result.stderr + "\n" + result.stdout)
        private.chmod(0o600)
        raise RuntimeError("Command failed; private diagnostic: " + str(private))
    return result.stdout


def config(path):
    return dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if line and not line.startswith("#")
    )


def write_config(path, updates):
    values = config(path) if path.exists() else {}
    values.update(updates)
    temporary = path.with_name(path.name + ".voice-tmp")
    with temporary.open("w") as stream:
        stream.write("".join(f"{key}={value}\n" for key, value in values.items()))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)


def inspect(name):
    return json.loads(run([*DOCKER, "inspect", name]))[0]


def save_backup(command, target):
    with target.open("wb") as stream:
        result = subprocess.run(command, stdout=stream, stderr=subprocess.PIPE, timeout=600)
    if result.returncode or not target.stat().st_size:
        raise RuntimeError("Backup failed: " + target.name)
    target.chmod(0o600)
    return hashlib.sha256(target.read_bytes()).hexdigest()


def compose(sha, image, deployment):
    os.environ.update(deployment, DEPLOY_ROOT=str(ROOT), RELEASE_SHA=sha, IMAGE_REF=image)
    return [
        *DOCKER,
        "compose",
        "--project-name",
        "involta-guide-production",
        "--env-file",
        str(ROOT / ".env.production"),
        "--env-file",
        str(ROOT / ".env.voice"),
        "-f",
        str(ROOT / "releases" / sha / "compose.yaml"),
        "-f",
        str(ROOT / "releases" / sha / "compose.voice.yaml"),
    ]


def healthy(command, service):
    identifier = run([*command, "ps", "-q", service]).strip()
    if not identifier:
        raise RuntimeError("Missing service: " + service)
    for _ in range(60):
        state = inspect(identifier)["State"]
        if state.get("Health", {}).get("Status") == "healthy":
            return
        if state.get("Status") in ("dead", "exited"):
            break
        time.sleep(2)
    raise RuntimeError("Service health timeout: " + service)


def main():
    action, target_sha = sys.argv[1:]
    assert action in ("prepare", "activate", "backfill", "report")
    assert re.fullmatch(r"[0-9a-f]{40}", target_sha)
    os.umask(0o077)
    with (
        open("/run/lock/involta-guide-production.lock", "a") as global_lock,
        open(ROOT / "deploy.lock", "a") as local_lock,
    ):
        fcntl.flock(global_lock, fcntl.LOCK_EX)
        fcntl.flock(local_lock, fcntl.LOCK_EX)
        web = inspect("involta-guide")
        assert web["Config"]["Labels"].get(OWNER) == "production"
        sha, image, _ = (ROOT / "current-release").read_text().split()
        assert web["Config"]["Image"] == image
        deployment = config(ROOT / "deploy.env")
        assert deployment["NETWORK_MODE"] == "host", "Only the inspected host topology is supported"
        app = config(ROOT / ".env.production")
        if action == "prepare":
            memory = dict(
                line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines()
            )
            assert int(memory["MemTotal"].split()[0]) >= 7 * 1024**2, "8 GiB VPS profile required"
            assert shutil.disk_usage(ROOT).free >= 6 * 1024**3, "At least 6 GiB free required"
            assert app.get("OPENAI_API_KEY"), "OPENAI_API_KEY required for text analysis"
            postgres = inspect("involta-guide-postgres")
            assert postgres["Config"]["Labels"].get(OWNER) == "production"
            pinned_postgres = postgres["Config"]["Image"]
            assert re.fullmatch(r"postgres@sha256:[0-9a-f]{64}", pinned_postgres)
            backup = ROOT / "backups" / ("voice-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
            backup.mkdir(parents=True, mode=0o700)
            hashes = {}
            hashes["guide.dump"] = save_backup(
                [
                    *DOCKER,
                    "exec",
                    "involta-guide-postgres",
                    "sh",
                    "-c",
                    'exec pg_dump -h 127.0.0.1 -p 15432 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc',
                ],
                backup / "guide.dump",
            )
            hashes["guide-audio.tar.gz"] = save_backup(
                [
                    *DOCKER,
                    "run",
                    "--rm",
                    "--user",
                    "0",
                    "--entrypoint",
                    "tar",
                    "-v",
                    deployment["AUDIO_VOLUME"] + ":/data:ro",
                    image,
                    "-czf",
                    "-",
                    "-C",
                    "/data",
                    ".",
                ],
                backup / "guide-audio.tar.gz",
            )
            for filename in (".env.production", "deploy.env"):
                shutil.copyfile(ROOT / filename, backup / filename)
                (backup / filename).chmod(0o600)
            voice_file = ROOT / ".env.voice"
            existing_voice = subprocess.run(
                [*DOCKER, "inspect", "involta-guide-production-voice-db-1"],
                capture_output=True,
                text=True,
            )
            if existing_voice.returncode == 0:
                voice_db = json.loads(existing_voice.stdout)[0]
                assert voice_file.exists() and app.get("VOICE_SERVICE_SECRET"), (
                    "Existing Voice configuration missing"
                )
                assert voice_db["Config"]["Labels"].get(OWNER) == "production"
                hashes["voice.dump"] = save_backup(
                    [
                        *DOCKER,
                        "exec",
                        voice_db["Id"],
                        "sh",
                        "-c",
                        'exec pg_dump -h 127.0.0.1 -p 55439 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc',
                    ],
                    backup / "voice.dump",
                )
                hashes["voice-audio.tar.gz"] = save_backup(
                    [
                        *DOCKER,
                        "run",
                        "--rm",
                        "--user",
                        "0",
                        "--entrypoint",
                        "tar",
                        "-v",
                        "involta-guide-production_voice-audio:/data:ro",
                        image,
                        "-czf",
                        "-",
                        "-C",
                        "/data",
                        ".",
                    ],
                    backup / "voice-audio.tar.gz",
                )
            if voice_file.exists():
                shutil.copyfile(voice_file, backup / ".env.voice")
                (backup / ".env.voice").chmod(0o600)
            else:
                password = secrets.token_hex(32)
                write_config(
                    voice_file,
                    {
                        "VOICE_DATABASE_URL": f"postgresql://voice:{password}@127.0.0.1:55439/voice",
                        "VOICE_POSTGRES_PASSWORD": password,
                        "VOICE_POSTGRES_IMAGE": pinned_postgres,
                    },
                )
            # Preserve existing Voice DB/audio; no volume creation/reset outside Compose.
            write_config(
                voice_file,
                {
                    "VOICE_FAKE_PROVIDER": "false",
                    "VOICE_ASR_BETA_ENABLED": "true",
                    "VOICE_TRANSCRIPTION_MODEL": "gigaam",
                    "VOICE_ASR_CPU_THREADS": "2",
                    "VOICE_ASR_MODELS_DIR": "/app/var/asr-models",
                    "VOICE_MIN_FREE_BYTES": str(256 * 1024**2),
                },
            )
            write_config(
                ROOT / ".env.production",
                {
                    "VOICE_SERVICE_SECRET": app.get("VOICE_SERVICE_SECRET")
                    or secrets.token_urlsafe(48),
                    "VOICE_URL": "http://127.0.0.1:13100",
                    "VOICE_ENABLED": app.get("VOICE_ENABLED", "false"),
                },
            )
            write_config(ROOT / "deploy.env", {"VOICE_STACK": "1"})
            release = ROOT / "releases" / target_sha
            release.mkdir(parents=True, exist_ok=True)
            (backup / "SHA256.json").write_text(json.dumps(hashes))
            (release / "voice-initial-rollout.json").write_text(
                json.dumps(
                    {
                        "previous_sha": sha,
                        "target_sha": target_sha,
                        "backup": str(backup),
                        "guide_sha256": hashes["guide.dump"],
                    }
                )
            )
            print(
                json.dumps(
                    {
                        "prepared": True,
                        "previous_sha": sha,
                        "target_sha": target_sha,
                        "backup": str(backup),
                        "cpu": os.cpu_count(),
                        "memory_kib": int(memory["MemTotal"].split()[0]),
                    }
                )
            )
            return
        assert sha == target_sha, "Another release is running; refusing to change it"
        command = compose(sha, image, deployment)
        for service in ("web", "worker", "voice-db", "voice-api", "voice-worker", "voice-consumer"):
            healthy(command, service)
        if action == "activate":
            assert config(ROOT / ".env.voice")["VOICE_FAKE_PROVIDER"] == "false"
            write_config(ROOT / ".env.production", {"VOICE_ENABLED": "true"})
            os.environ["VOICE_INTAKE"] = "true"
            run([*command, "up", "-d", "--no-build", "--no-deps", "web"])
            healthy(command, "web")
            print(
                json.dumps(
                    {"enabled": True, "revision": sha, "asr_beta": True, "fake_provider": False}
                )
            )
        elif action == "backfill":
            for service, cli in (("web", "guide-voice-asr-backfill"), ("voice-api", "voice")):
                args = [cli] if service == "web" else [cli, "asr-backfill"]
                for execute in (False, True):
                    result = json.loads(
                        run(
                            [
                                *command,
                                "exec",
                                "-T",
                                service,
                                *args,
                                *(["--execute"] if execute else []),
                            ],
                            timeout=900,
                        )
                    )
                    print(json.dumps({"service": service, "inventory": result}, ensure_ascii=False))
        else:
            result = json.loads(
                run([*command, "exec", "-T", "voice-api", "voice", "asr-report"], timeout=600)
            )
            print(
                json.dumps({"healthy": True, "revision": sha, "beta": result}, ensure_ascii=False)
            )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            json.dumps(
                {
                    "failed": type(error).__name__,
                    "stage": sys.argv[1] if len(sys.argv) > 1 else "unknown",
                    "message": "Operation stopped; private server diagnostics retained",
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
