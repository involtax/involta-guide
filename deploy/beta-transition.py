"""One-time, coordinated beta_core transition. No nginx, host packages or foreign services."""

import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("voice_rollout", HERE / "voice-rollout.py")
rollout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollout)
ROOT = Path("/opt/involta-guide")
DOCKER = ["docker", "--host=unix:///var/run/docker.sock"]
WRITERS = ("web", "worker", "voice-api", "voice-worker", "voice-consumer")
OWNER = "com.involta.guide.owner"


def execute(operation):
    """No shutdown before preflight; no old writers against migrated databases."""
    operation.preflight()
    stopped = changed = False
    try:
        stopped = True
        operation.stop()
        operation.backup()
        changed = True
        operation.migrate()
        operation.start()
        operation.verify()
        operation.commit()
    except BaseException:
        if changed:
            operation.restore()  # Failure here intentionally leaves all writers stopped.
        if stopped:
            operation.restart_old()
        raise


class Transition:
    def __init__(self, request):
        self.sha, self.image = request["sha"], request["image"]
        self.run_id = request["run_id"]
        assert re.fullmatch(r"[0-9a-f]{40}", self.sha)
        assert re.fullmatch(r"ghcr.io/involtax/involta-guide@sha256:[0-9a-f]{64}", self.image)
        assert re.fullmatch(r"[0-9]+-[0-9]+", self.run_id)
        assert re.fullmatch(r"[a-zA-Z0-9_-]+", request["actor"])
        self.request = request
        self.redactions = [request["token"]]
        self.stage = "preflight"
        self.counter = 0
        self.backup_dir = ROOT / "backups" / ("beta-" + self.run_id)
        self.release = ROOT / "releases" / self.sha

    def log(self, **values):
        print(json.dumps({"stage": self.stage, **values}, ensure_ascii=False), flush=True)

    def run(self, args, *, data=None, output=None, timeout=600):
        result = subprocess.run(
            args,
            input=data,
            stdout=output or subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        if result.returncode:
            diagnostic = result.stderr.decode(errors="replace")
            if output is None:
                diagnostic += "\n" + result.stdout.decode(errors="replace")
            for secret in sorted(self.redactions, key=len, reverse=True):
                if secret:
                    diagnostic = diagnostic.replace(secret, "[redacted]")
            raise RuntimeError(
                f"{self.stage}: command exit {result.returncode}: {diagnostic[-5000:]}"
            )
        return result.stdout.decode() if output is None else ""

    def inspect(self, name):
        return json.loads(self.run([*DOCKER, "inspect", name]))[0]

    def oneoff(self, command, service, *args, **kwargs):
        self.counter += 1
        return self.run(
            [
                *command,
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "--name",
                f"involta-guide-beta-{self.run_id}-{self.counter}",
                "--label",
                f"com.involta.guide.beta-operation={self.run_id}",
                service,
                *args,
            ],
            **kwargs,
        )

    def preflight(self):
        assert os.geteuid() == 0, "Root required"
        self.old_state = (ROOT / "current-release").read_text()
        self.old_sha, self.old_image, self.old_id = self.old_state.split()
        assert re.fullmatch(r"[0-9a-f]{40}", self.old_sha)
        assert self.old_sha != self.sha, "Already deployed; use ordinary health checks"
        self.deployment = rollout.config(ROOT / "deploy.env")
        assert self.deployment["NETWORK_MODE"] == "host", "Only verified host topology supported"
        assert self.deployment.get("VOICE_STACK") == "1"
        for filename in (".env.production", ".env.voice"):
            values = rollout.config(ROOT / filename)
            self.redactions.extend(
                v
                for k, v in values.items()
                if re.search(r"KEY|SECRET|PASSWORD|TOKEN|DATABASE_URL", k)
            )
        self.old = self.compose(self.old_sha, self.old_image)
        self.previous_state = (
            (ROOT / "previous-release").read_bytes()
            if (ROOT / "previous-release").exists()
            else None
        )
        self.ids = {}
        for service in (*WRITERS, "voice-db"):
            identifier = self.run([*self.old, "ps", "-q", service]).strip()
            assert identifier, "Missing owned service: " + service
            details = self.inspect(identifier)
            assert details["Config"]["Labels"].get(OWNER) == "production", (
                "Foreign service: " + service
            )
            if service in WRITERS:
                assert details["Config"]["Image"] == self.old_image, (
                    "Mixed running images: " + service
                )
            self.ids[service] = identifier
        guide_db = self.inspect("involta-guide-postgres")
        assert guide_db["Config"]["Labels"].get(OWNER) == "production"
        self.volumes = {}
        for service, destination in (("web", "/app/var"), ("voice-api", "/app/var/voice")):
            mount = next(
                m
                for m in self.inspect(self.ids[service])["Mounts"]
                if m["Destination"] == destination
            )
            assert mount["Type"] == "volume" and re.fullmatch(r"[a-zA-Z0-9_.-]+", mount["Name"])
            self.volumes[service] = mount["Name"]
        assert self.volumes["web"] == self.deployment["AUDIO_VOLUME"]
        assert self.volumes["voice-api"] == "involta-guide-production_voice-audio"
        assert shutil.disk_usage(ROOT).free >= 6 * 1024**3, "Need 6 GiB free before transition"
        self.run(
            [*DOCKER, "login", "ghcr.io", "--username", self.request["actor"], "--password-stdin"],
            data=self.request["token"].encode(),
        )
        self.run([*DOCKER, "pull", self.image])
        assert self.inspect_image_revision() == self.sha, "Image revision mismatch"
        self.release.mkdir(parents=True, exist_ok=True)
        for source, target in (
            ("compose.host.yaml", "compose.yaml"),
            ("compose.voice.host.yaml", "compose.voice.yaml"),
        ):
            source_bytes = (HERE / source).read_bytes()
            target_path = self.release / target
            if target_path.exists():
                assert target_path.read_bytes() == source_bytes, "Existing release template differs"
            else:
                target_path.write_bytes(source_bytes)
                target_path.chmod(0o600)
        self.new = self.compose(self.sha, self.image)
        self.run([*self.new, "config", "--quiet"])
        # Inspect actual schemas without importing or starting application workers.
        self.inventory = self.guide_inventory(self.old)
        assert self.inventory["revision"] in ("admin_routes", "review_urls"), self.inventory
        assert self.voice_sql("SELECT max(version) FROM voice_schema_versions").strip() == "3"
        schemas = self.voice_sql(
            "SELECT nspname FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname <> 'information_schema'"
        ).split()
        assert schemas == ["public"], "Voice database contains unexpected schemas"
        tables = set(
            self.voice_sql("SELECT tablename FROM pg_tables WHERE schemaname='public'").split()
        )
        expected = {
            "voice_schema_versions",
            "submissions",
            "runs",
            "stages",
            "outbox",
            "delivery_ack",
            "voice_settings",
            "provider_calls",
            "intake_windows",
            "archive_map",
            "admin_events",
            "asr_candidates",
            "asr_evaluations",
            "asr_evaluation_events",
            "asr_run_requests",
        }
        assert tables == expected, "Voice database contains unexpected tables"
        self.run([*self.new, "run", "--rm", "--no-deps", "voice-prepare-model"], timeout=1200)
        self.log(
            preflight="passed",
            previous_sha=self.old_sha,
            target_sha=self.sha,
            inventory=self.inventory,
        )

    def compose(self, sha, image):
        # Freeze the public image/revision per command; rollout.compose updates os.environ.
        return [
            "env",
            f"RELEASE_SHA={sha}",
            f"IMAGE_REF={image}",
            *rollout.compose(sha, image, self.deployment),
        ]

    def inspect_image_revision(self):
        return self.run(
            [
                *DOCKER,
                "image",
                "inspect",
                "--format",
                '{{index .Config.Labels "org.opencontainers.image.revision"}}',
                self.image,
            ]
        ).strip()

    def voice_sql(self, sql):
        # This is the dedicated Voice database, not the shared Guide connection.
        return self.run(
            [
                *DOCKER,
                "exec",
                "-i",
                self.ids["voice-db"],
                "psql",
                "-X",
                "-v",
                "ON_ERROR_STOP=1",
                "-h",
                "127.0.0.1",
                "-p",
                "55439",
                "-U",
                "voice",
                "-d",
                "voice",
                "-At",
            ],
            data=sql.encode(),
        )

    def guide_inventory(self, command):
        code = """import json
from sqlalchemy import text
from involta_guide.config import Settings
from involta_guide.infrastructure.persistence.unit_of_work import create_database
s=Settings(); e=create_database(s)
with e.connect() as c:
 print(json.dumps({'revision':c.execute(text('SELECT version_num FROM alembic_version')).scalar_one(),'locations':c.execute(text('SELECT count(*) FROM locations')).scalar_one(),'owned_restaurants':c.execute(text('SELECT count(*) FROM restaurants r JOIN locations l ON l.id=r.location_id WHERE NOT l.is_hub')).scalar_one(),'stars':c.execute(text('SELECT count(*) FROM restaurants r JOIN locations l ON l.id=r.location_id WHERE NOT l.is_hub AND r.alex_star')).scalar_one()}))
e.dispose()
"""
        return json.loads(self.oneoff(command, "web", "python", "-", data=code.encode()))

    def stop(self):
        self.stage = "stop-owned-writers"
        self.run([*self.old, "stop", "--timeout", "240", *WRITERS], timeout=400)
        self.log(stopped=list(WRITERS))

    def save(self, name, command):
        target = self.backup_dir / name
        with target.open("xb") as stream:
            self.run(command, output=stream)
            stream.flush()
            os.fsync(stream.fileno())
        assert target.stat().st_size > 0, "Empty backup: " + name
        return hashlib.sha256(target.read_bytes()).hexdigest()

    def backup(self):
        self.stage = "consistent-backup"
        self.inventory = self.guide_inventory(self.old)
        self.backup_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        hashes = {}
        hashes["guide.dump"] = self.save(
            "guide.dump",
            [
                *self.new,
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "web",
                "python",
                "-m",
                "involta_guide.infrastructure.persistence.release_backup",
                "backup",
            ],
        )
        hashes["voice.dump"] = self.save(
            "voice.dump",
            [
                *DOCKER,
                "exec",
                self.ids["voice-db"],
                "pg_dump",
                "-h",
                "127.0.0.1",
                "-p",
                "55439",
                "-U",
                "voice",
                "-d",
                "voice",
                "--no-owner",
                "--no-privileges",
                "-Fc",
            ],
        )
        self.run(
            [*DOCKER, "exec", "-i", self.ids["voice-db"], "pg_restore", "--list"],
            data=(self.backup_dir / "voice.dump").read_bytes(),
        )
        for service, volume in self.volumes.items():
            name = service + "-audio.tar.gz"
            hashes[name] = self.save(
                name,
                [
                    *DOCKER,
                    "run",
                    "--rm",
                    "--user",
                    "0",
                    "--entrypoint",
                    "tar",
                    "-v",
                    volume + ":/data:ro",
                    self.image,
                    "-czf",
                    "-",
                    "-C",
                    "/data",
                    ".",
                ],
            )
        for name in (
            ".env.production",
            ".env.voice",
            "deploy.env",
            "current-release",
            "previous-release",
        ):
            if (ROOT / name).exists():
                shutil.copyfile(ROOT / name, self.backup_dir / name)
        (self.backup_dir / "SHA256.json").write_text(json.dumps(hashes))
        self.log(backup=str(self.backup_dir), files=list(hashes))

    def migrate(self):
        self.stage = "migrate-and-cleanup"
        self.oneoff(self.new, "web", "alembic", "upgrade", "head")
        self.oneoff(self.new, "voice-migrate", "voice", "migrate")
        for service, cli in (("web", "guide"), ("voice-api", "voice")):
            self.oneoff(self.new, service, cli, "beta-cleanup-files", "--execute")
            result = self.oneoff(self.new, service, cli, "beta-cleanup-files")
            if service == "web":
                assert result.strip().startswith("Files pending: 0;"), result
                pending = 0
            else:
                pending = json.loads(result)["pending_files"]
                assert pending == 0, result
            self.log(service=service, pending_files=pending)
        current = self.guide_inventory(self.new)
        assert current["revision"] == "review_publication", current

        for key in ("locations", "owned_restaurants", "stars"):
            assert current[key] == self.inventory[key], f"Inventory changed: {key}"
        assert self.voice_sql("SELECT max(version) FROM voice_schema_versions").strip() == "9"
        assert self.voice_sql("SELECT count(*) FROM purge_files").strip() == "0"
        self.log(inventory=current)

    def start(self):
        self.stage = "start-target"
        self.run([*self.new, "up", "-d", "--no-build", "--no-deps", *WRITERS])
        for service in WRITERS:
            rollout.healthy(self.new, service)
        self.log(healthy=list(WRITERS))

    def verify(self):
        self.stage = "verify-target"
        self.run(
            [
                "bash",
                str(HERE / "check-release.sh"),
                "https://involta.ru/guide/",
                self.sha,
                "external",
            ]
        )
        for service in WRITERS:
            identifier = self.run([*self.new, "ps", "-q", service]).strip()
            assert self.inspect(identifier)["Config"]["Image"] == self.image
        self.log(https="passed", immutable_sha=self.sha)

    def commit(self):
        self.stage = "commit-release"
        temporary = ROOT / "current-release.beta-tmp"
        temporary.write_text(f"{self.sha} {self.image} {self.run_id}\n")
        (ROOT / "previous-release").write_text(self.old_state)
        temporary.replace(ROOT / "current-release")
        self.log(deployed=True, revision=self.sha, backup=str(self.backup_dir))

    def stop_operations(self):
        identifiers = self.run(
            [
                *DOCKER,
                "ps",
                "-q",
                "--filter",
                f"label=com.involta.guide.beta-operation={self.run_id}",
            ]
        ).split()
        for identifier in identifiers:
            assert self.inspect(identifier)["Config"]["Labels"].get(OWNER) == "production"
            self.run([*DOCKER, "stop", "--time", "10", identifier])

    def restore(self):
        self.stage = "restore-consistent-backup"
        self.stop_operations()
        self.run([*self.new, "stop", "--timeout", "240", *WRITERS], timeout=400)
        hashes = json.loads((self.backup_dir / "SHA256.json").read_text())
        for name, digest in hashes.items():
            assert hashlib.sha256((self.backup_dir / name).read_bytes()).hexdigest() == digest
        self.oneoff(
            self.new,
            "web",
            "python",
            "-m",
            "involta_guide.infrastructure.persistence.release_backup",
            "restore",
            data=(self.backup_dir / "guide.dump").read_bytes(),
        )
        self.voice_sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public AUTHORIZATION voice;")
        self.run(
            [
                *DOCKER,
                "exec",
                "-i",
                self.ids["voice-db"],
                "pg_restore",
                "--exit-on-error",
                "--single-transaction",
                "--no-owner",
                "--no-privileges",
                "-h",
                "127.0.0.1",
                "-p",
                "55439",
                "-U",
                "voice",
                "-d",
                "voice",
            ],
            data=(self.backup_dir / "voice.dump").read_bytes(),
        )
        for service, volume in self.volumes.items():
            self.run(
                [
                    *DOCKER,
                    "run",
                    "--rm",
                    "--user",
                    "0",
                    "--entrypoint",
                    "python",
                    "-v",
                    volume + ":/data",
                    self.image,
                    "-c",
                    "from pathlib import Path; import shutil; [(shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()) for p in Path('/data').iterdir()]",
                ]
            )
            self.run(
                [
                    *DOCKER,
                    "run",
                    "--rm",
                    "-i",
                    "--user",
                    "0",
                    "--entrypoint",
                    "tar",
                    "-v",
                    volume + ":/data",
                    self.image,
                    "-xzf",
                    "-",
                    "-C",
                    "/data",
                ],
                data=(self.backup_dir / (service + "-audio.tar.gz")).read_bytes(),
            )
        (ROOT / "current-release").write_text(self.old_state)
        previous = ROOT / "previous-release"
        if self.previous_state is None:
            previous.unlink(missing_ok=True)
        else:
            previous.write_bytes(self.previous_state)
        assert self.guide_inventory(self.new) == self.inventory
        assert self.voice_sql("SELECT max(version) FROM voice_schema_versions").strip() == "3"
        self.log(restored=True)

    def restart_old(self):
        self.stage = "restart-previous"
        self.stop_operations()
        self.run([*self.old, "up", "-d", "--no-build", "--no-deps", *WRITERS])
        for service in WRITERS:
            rollout.healthy(self.old, service)
        self.log(rolled_back=True, revision=self.old_sha)


def main():
    os.umask(0o077)
    for key in (
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_TLS",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
    ):
        os.environ.pop(key, None)
    operation = Transition(json.load(sys.stdin))

    def interrupted(signum, frame):
        raise RuntimeError(f"Transition interrupted: {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    with tempfile.TemporaryDirectory(prefix=".beta-registry-", dir=ROOT) as registry:
        os.environ["DOCKER_CONFIG"] = registry
        with (
            open("/run/lock/involta-guide-production.lock", "a") as global_lock,
            open(ROOT / "deploy.lock", "a") as local_lock,
        ):
            fcntl.flock(global_lock, fcntl.LOCK_EX)
            fcntl.flock(local_lock, fcntl.LOCK_EX)
            execute(operation)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            json.dumps({"failed": type(error).__name__, "message": str(error)}, ensure_ascii=False),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
