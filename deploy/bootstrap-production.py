#!/usr/bin/env python3
"""Initial provisioning only. All server actions require explicit workflow approval.

Stdlib only; compatible with Python 3.10+. Never logs command output or credentials.
"""

import base64
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from nginx_config import render, select_server, servers

ROOT = Path("/opt/involta-guide")
OWNER = "com.involta.guide.owner"
LABEL = f"{OWNER}=production"
SCRIPT = Path(__file__).resolve().parent
GLOBAL_LOCK = Path("/run/lock/involta-guide-production.lock")
POLICY = Path("/usr/sbin/policy-rc.d")
DOCKER_KEY = Path("/etc/apt/keyrings/involta-docker.asc")
DOCKER_SOURCE = Path("/etc/apt/sources.list.d/involta-docker.sources")
DOCKER_DAEMON = Path("/etc/docker/daemon.json")


class Failure(RuntimeError):
    pass


def run(args, *, data=None, check=True, timeout=300, env=None):
    result = subprocess.run(
        args,
        input=data,
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env,
    )
    if check and result.returncode:
        # Output may contain environment values, registry credentials or nginx config.
        raise Failure(
            f"{Path(args[0]).name} failed (exit {result.returncode}); command output withheld"
        )
    return result


def docker(*args, **kwargs):
    return run(["docker", "--host=unix:///var/run/docker.sock", *args], **kwargs)


def owned(kind, name):
    result = docker(kind, "inspect", name, check=False)
    if result.returncode:
        return False
    obj = json.loads(result.stdout)[0]
    labels = obj.get("Labels") or obj.get("Config", {}).get("Labels") or {}
    if labels.get(OWNER) != "production":
        raise Failure(f"Existing {kind} {name} is not owned by Guide; nothing will be adopted")
    return True


def baseline():
    result = run(
        [
            "curl",
            "--silent",
            "--show-error",
            "--output",
            "/dev/null",
            "--max-time",
            "20",
            "--proto",
            "=https",
            "--write-out",
            "%{http_code} %{redirect_url}",
            "https://involta.ru/",
        ]
    )
    if not re.match(r"[23][0-9]{2}( |$)", result.stdout):
        raise Failure("Existing HTTPS homepage is not healthy; refusing to change nginx")
    return result.stdout


def nginx_candidates():
    candidates = []
    if (
        shutil.which("nginx")
        and run(["systemctl", "is-active", "--quiet", "nginx"], check=False).returncode == 0
    ):
        pid = run(["systemctl", "show", "nginx", "--property=MainPID", "--value"]).stdout.strip()
        if not pid.isdigit() or pid == "0":
            raise Failure("Cannot determine nginx master PID")
        process = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode()
        if re.search(r"(?:^|\s)-(?:c|p)", process):
            raise Failure(
                "nginx uses a custom -c/-p startup path; automatic default-config editing is disabled"
            )
        candidates.append((["nginx"], None))
    if shutil.which("docker"):
        ids = docker("ps", "--format", "{{.ID}} {{.Image}} {{.Names}}", check=False)
        if ids.returncode:
            raise Failure(
                "Docker is installed but its daemon is unavailable; refusing to start/reconfigure an existing daemon"
            )
        for line in ids.stdout.splitlines():
            if "nginx" in line.lower():
                info = json.loads(docker("inspect", line.split()[0]).stdout)[0]
                args = info.get("Args", [])
                if any(arg.startswith(("-c", "-p")) for arg in args):
                    raise Failure(
                        "Container nginx uses custom -c/-p startup flags; safe default-config mapping is unavailable"
                    )
                candidates.append(
                    (
                        [
                            "docker",
                            "--host=unix:///var/run/docker.sock",
                            "exec",
                            line.split()[0],
                            "nginx",
                        ],
                        line.split()[0],
                    )
                )
    valid = []
    for command, container in candidates:
        dump = run([*command, "-T"], check=False)
        if container and dump.returncode in (126, 127):
            continue  # e.g. nginx-exporter, not an nginx server
        if dump.returncode:
            raise Failure("Existing nginx -T failed; repair the current site before bootstrap")
        try:
            selected = select_server(dump.stdout)
        except ValueError as exc:
            if "found 0" in str(exc):
                continue
            raise Failure(str(exc)) from exc
        valid.append((command, container, selected))
    if len(valid) != 1:
        raise Failure(
            "Cannot identify one active nginx serving involta.ru:443 (host systemd service or nginx-named Docker container required)"
        )
    command, container, selected = valid[0]
    target = Path(selected.file)
    frontend, upstream = "host", "127.0.0.1:3080"
    if container:
        meta = json.loads(docker("inspect", container).stdout)[0]
        mounts = sorted(meta["Mounts"], key=lambda m: len(m["Destination"]), reverse=True)
        for mount in mounts:
            destination = Path(mount["Destination"])
            if target == destination or destination in target.parents:
                if mount["Type"] != "bind":
                    raise Failure(
                        "nginx config is not bind-mounted; cannot persistently edit it without recreating its container"
                    )
                target = Path(mount["Source"]) / target.relative_to(destination)
                break
        else:
            raise Failure(
                "nginx config is inside a container image; a bind mount is required for safe editing"
            )
        mode = meta["HostConfig"]["NetworkMode"]
        if mode != "host":
            networks = meta["NetworkSettings"]["Networks"]
            usable = [name for name in networks if name not in ("bridge", "none", "host")]
            if len(usable) != 1:
                raise Failure(
                    "Containerized nginx requires exactly one existing user-defined network; no foreign networks will be modified"
                )
            frontend, upstream = usable[0], "involta-guide:3000"
            info = json.loads(docker("network", "inspect", frontend).stdout)[0]
            if info.get("Driver") != "bridge" or info.get("Internal"):
                raise Failure("nginx network must be a non-internal bridge network")
    target = target.resolve(strict=True)
    if not target.is_file() or not os.access(target, os.W_OK):
        raise Failure("The selected nginx configuration file is not writable")
    original = target.read_text()
    # -T adds separators/newlines. Locate the exact server in the actual file again.
    local = [
        s
        for s in servers(selected.file, original)
        if original[s.opening : s.closing].strip()
        == selected.text[selected.opening : selected.closing].strip()
    ]
    if len(local) != 1:
        raise Failure(
            "nginx configuration changed or server block cannot be mapped safely to its source"
        )
    proposed = render(original, local[0], upstream)
    return command, target, original, proposed, frontend, container


def validate_request(request):
    if not re.fullmatch(r"[0-9a-f]{40}", request.get("sha", "")):
        raise Failure("Expected full lowercase commit SHA")
    if not re.fullmatch(r"ghcr\.io/[a-z0-9._/-]+@sha256:[0-9a-f]{64}", request.get("image", "")):
        raise Failure("Guide image must be pinned by GHCR digest")
    if not re.fullmatch(r"postgres@sha256:[0-9a-f]{64}", request.get("postgres_image", "")):
        raise Failure("PostgreSQL image must use the official postgres repository and a digest")
    if not re.fullmatch(r"[0-9]+-[0-9]+", request.get("run_id", "")):
        raise Failure("Invalid run ID")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", request.get("actor", "")):
        raise Failure("Invalid registry username")
    if not request.get("token"):
        raise Failure("Missing GHCR credential")
    for name in ("openai_api_key", "admin_password", "admin_password_hash"):
        value = request.get(name, "")
        if not isinstance(value, str) or any(c in value for c in ("\n", "\r", "\x00", "'", "\\")):
            raise Failure(f"Invalid {name}: multiline/escaped values are not supported")
    if request.get("admin_password") and len(request["admin_password"]) < 12:
        raise Failure("ADMIN_PASSWORD must contain at least 12 characters")
    if not isinstance(request.get("alex_secret", ""), str):
        raise Failure("ALEX_SECRET must be a string")


def validate_os_release(release):
    name = release.get("PRETTY_NAME", release.get("ID", "unknown"))
    print(
        f"Detected OS: {name}; ID={release.get('ID')}; VERSION_ID={release.get('VERSION_ID')}; codename={release.get('VERSION_CODENAME')}",
        flush=True,
    )
    if release.get("ID") not in ("ubuntu", "debian"):
        raise Failure(
            f"Unsupported package manager for {name}; bootstrap requires Ubuntu/Debian APT; no changes made"
        )
    if not release.get("VERSION_CODENAME", "").isalnum():
        raise Failure(f"Cannot determine safe OS codename for {name}; no changes made")


def check_docker_repository(release):
    url = f"https://download.docker.com/linux/{release['ID']}/dists/{release['VERSION_CODENAME']}/Release"
    result = run(
        [
            "curl",
            "--fail",
            "--silent",
            "--show-error",
            "--head",
            "--max-time",
            "20",
            "--output",
            "/dev/null",
            "--write-out",
            "%{http_code}",
            url,
        ],
        check=False,
    )
    if result.returncode or result.stdout.strip() != "200":
        raise Failure(
            f"Official Docker repository unavailable: {url} (HTTP {result.stdout.strip() or 'unknown'}); no package changes made"
        )


def preflight(request):
    validate_request(request)
    if os.geteuid() != 0:
        raise Failure("Root or passwordless sudo is required; no changes made")
    for name in ("curl", "systemctl", "flock", "timeout", "apt-get", "dpkg", "ps"):
        if not shutil.which(name):
            raise Failure(f"Missing prerequisite {name}; no changes made")
    release = dict(
        line.split("=", 1)
        for line in Path("/etc/os-release").read_text().splitlines()
        if "=" in line
    )
    release = {k: v.strip('"') for k, v in release.items()}
    validate_os_release(release)
    if not Path("/run/systemd/system").exists():
        raise Failure("Bootstrap requires systemd; refusing to change another init system")
    if ROOT.is_symlink() or (ROOT.exists() and not (ROOT / ".bootstrap-owner").is_file()):
        raise Failure("/opt/involta-guide exists without a Guide ownership marker")
    if (ROOT / "current-release").exists():
        raise Failure(
            "Guide is already deployed; use deploy-production.yml (bootstrap is initial setup only)"
        )
    db_env, app_env = ROOT / ".env.database", ROOT / ".env.production"
    if db_env.exists() != app_env.exists():
        raise Failure("Incomplete secret-file pair; no credentials will be regenerated")
    for path in (db_env, app_env, ROOT / "deploy.env"):
        if path.is_symlink():
            raise Failure("Symlinked secret/deployment files are not supported")
    docker_present = shutil.which("docker") is not None
    if docker_present:
        docker("info")
        for name in (
            "involta-guide",
            "involta-guide-postgres",
            "involta-guide-production-worker-1",
        ):
            if owned("container", name) and name != "involta-guide-postgres":
                raise Failure(f"Existing {name} has no release record; refusing to replace it")
        for kind, names in (("volume", ("involta-guide-audio", "involta-guide-postgres-data")),):
            for name in names:
                exists = owned(kind, name)
                if exists and name == "involta-guide-postgres-data" and not db_env.exists():
                    raise Failure(
                        "Existing database volume has no credential files; refusing to replace credentials"
                    )
    compose_present = docker_present and docker("compose", "version", check=False).returncode == 0
    if not docker_present or not compose_present:
        check_docker_repository(release)
        if POLICY.exists():
            raise Failure(
                "Existing policy-rc.d requires operator-specific package handling; no changes made"
            )
        if DOCKER_KEY.exists() or DOCKER_SOURCE.exists():
            raise Failure("Guide Docker repository files already exist; refusing to overwrite them")
    nginx = nginx_candidates()
    # Host networking avoids Docker firewall/forwarding changes entirely.
    if nginx[4] == "host":
        for port in (3080, 15432):
            if port == 15432 and docker_present and owned("container", "involta-guide-postgres"):
                continue
            with socket.socket() as probe:
                try:
                    probe.bind(("127.0.0.1", port))
                except OSError as exc:
                    raise Failure(
                        f"Required Guide loopback port {port} is occupied; no changes made"
                    ) from exc
    if not docker_present:
        if DOCKER_DAEMON.exists():
            raise Failure(
                "Docker daemon.json already exists without an available Engine; refusing to overwrite it"
            )
        for service in ("docker.socket", "containerd", "podman", "kubelet"):
            if run(["systemctl", "is-active", "--quiet", service], check=False).returncode == 0:
                raise Failure(
                    f"Existing {service} requires manual assessment; no runtime will be restarted"
                )
    if shutil.disk_usage("/opt").free < 3 * 1024**3:
        raise Failure("Less than 3 GiB available in /opt")
    home = baseline()
    # Detect distro/conflicting installs before apt is allowed to change anything.
    installed = run(["dpkg-query", "-W", "-f=${Package} ${Status}\n"], check=False).stdout
    conflicts = [
        line.split()[0]
        for line in installed.splitlines()
        if line.endswith("install ok installed")
        and line.split()[0]
        in {"docker.io", "docker-compose", "podman-docker", "containerd", "runc"}
    ]
    if (not docker_present or not compose_present) and conflicts:
        raise Failure(
            "Conflicting packages present: "
            + ", ".join(conflicts)
            + "; automatic removal is forbidden"
        )
    return release, nginx, home, docker_present, compose_present


def install_docker(release, present, compose_present):
    if present and compose_present:
        return "Existing Engine and Compose retained"
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive", "NEEDRESTART_MODE": "l"}
    # Do not let package hooks restart unrelated services.
    policy = POLICY
    if policy.exists():
        raise Failure(
            "Existing policy-rc.d requires operator-specific package handling; refusing to replace it"
        )
    policy.write_text("#!/bin/sh\nexit 101\n")
    policy.chmod(0o755)
    try:
        run(["apt-get", "update"], env=env)
        run(
            [
                "apt-get",
                "install",
                "-y",
                "--no-install-recommends",
                "ca-certificates",
                "curl",
                "gnupg",
            ],
            env=env,
            timeout=600,
        )
        key = DOCKER_KEY
        source = DOCKER_SOURCE
        if key.exists() or source.exists():
            raise Failure(
                "Guide Docker repository files already exist; refusing to overwrite administrator configuration"
            )
        key.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        run(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--proto",
                "=https",
                "--max-time",
                "60",
                "--output",
                str(key),
                f"https://download.docker.com/linux/{release['ID']}/gpg",
            ]
        )
        key.chmod(0o644)
        fingerprint = run(["gpg", "--batch", "--show-keys", "--with-colons", str(key)]).stdout
        fingerprints = [
            line.split(":")[9]
            for line in fingerprint.splitlines()
            if line.startswith("fpr:") and len(line.split(":")) > 9
        ]
        if "9DC858229FC7DD38854AE2D88D81803C0EBFCD88" not in fingerprints:
            raise Failure("Official Docker signing key fingerprint did not match")
        arch = run(["dpkg", "--print-architecture"]).stdout.strip()
        source.write_text(
            f"Types: deb\nURIs: https://download.docker.com/linux/{release['ID']}\nSuites: {release['VERSION_CODENAME']}\nComponents: stable\nArchitectures: {arch}\nSigned-By: {key}\n"
        )
        source.chmod(0o644)
        run(["apt-get", "update"], env=env)
        packages = (
            ["docker-compose-plugin"]
            if present
            else [
                "docker-ce",
                "docker-ce-cli",
                "containerd.io",
                "docker-buildx-plugin",
                "docker-compose-plugin",
            ]
        )
        run(
            ["apt-get", "install", "-y", "--no-install-recommends", *packages], env=env, timeout=900
        )
    finally:
        policy.unlink(missing_ok=True)
    if not present:
        # Only a fresh daemon: never enable forwarding or let Docker change firewall/NAT.
        DOCKER_DAEMON.parent.mkdir(mode=0o755, exist_ok=True)
        write_private(
            DOCKER_DAEMON,
            json.dumps(
                {
                    "bridge": "none",
                    "iptables": False,
                    "ip6tables": False,
                    "ip-forward": False,
                    "ip-masq": False,
                }
            ),
        )
        run(["systemctl", "enable", "--now", "docker"])
    docker("info")
    docker("compose", "version")
    return "Official Docker repository packages installed"


def write_private(path, text):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write(text)


def proxy_ip(frontend, container):
    if frontend == "host":
        return "127.0.0.1"
    info = json.loads(docker("inspect", container).stdout)[0]
    return info["NetworkSettings"]["Networks"][frontend]["IPAddress"]


def initialize(request, frontend, container=None):
    for name in ("involta-guide-audio", "involta-guide-postgres-data"):
        if not owned("volume", name):
            docker("volume", "create", "--label", LABEL, name)
    db_env, app_env = ROOT / ".env.database", ROOT / ".env.production"
    if db_env.exists() != app_env.exists():
        raise Failure(
            "Incomplete secret-file pair; refusing to regenerate credentials for existing data"
        )
    if not db_env.exists():
        password = secrets.token_urlsafe(48)
        write_private(
            db_env, f"POSTGRES_DB=guide\nPOSTGRES_USER=guide\nPOSTGRES_PASSWORD='{password}'\n"
        )
        values = {
            "DATABASE_URL": f"postgresql://guide:{password}@"
            + ("127.0.0.1:15432" if frontend == "host" else "involta-guide-postgres:5432")
            + "/guide",
            "SESSION_SECRET": secrets.token_urlsafe(48),
            "ADMIN_PASSWORD": request.get("admin_password") or secrets.token_urlsafe(32),
            "ADMIN_PASSWORD_HASH": request.get("admin_password_hash", ""),
            # Encode for env-file transport so quotes, Unicode and newlines remain literal.
            "ALEX_SECRET_BASE64": base64.b64encode(
                request.get("alex_secret", "").encode("utf-8")
            ).decode("ascii"),
            "OPENAI_API_KEY": request.get("openai_api_key", ""),
            "SITE_URL": "https://involta.ru/guide",
            "BASE_PATH": "/guide",
            "COOKIE_SECURE": "true",
            "DATA_DIR": "/app/var",
            "TRUSTED_PROXY_IPS": proxy_ip(frontend, container),
        }
        write_private(app_env, "".join(f"{k}='{v}'\n" for k, v in values.items()))
    env_file = ROOT / "deploy.env"
    contents = f"AUDIO_VOLUME=involta-guide-audio\nGUIDE_HTTP_PORT=3080\nBASE_PATH=/guide\nNGINX_NETWORK={frontend}\nNETWORK_MODE={'host' if frontend == 'host' else 'bridge'}\nPOSTGRES_IMAGE={request['postgres_image']}\n"
    if env_file.exists() and env_file.read_text() != contents:
        raise Failure("Existing deployment options differ; refusing to overwrite them")
    if not env_file.exists():
        write_private(env_file, contents)
    shutil.copyfile(
        SCRIPT / ("compose.database-host.yaml" if frontend == "host" else "compose.database.yaml"),
        ROOT / "compose.database.yaml",
    )


def compose_env(request, frontend):
    return {
        **os.environ,
        "DEPLOY_ROOT": str(ROOT),
        "POSTGRES_IMAGE": request["postgres_image"],
        "IMAGE_REF": request["image"],
        "RELEASE_SHA": request["sha"],
        "AUDIO_VOLUME": "involta-guide-audio",
        "GUIDE_HTTP_PORT": "3080",
        "BASE_PATH": "/guide",
        "NGINX_NETWORK": frontend,
        "NETWORK_MODE": "host" if frontend == "host" else "bridge",
    }


def compose(request, frontend, *args):
    return docker(
        "compose",
        "--project-name",
        "involta-guide-production",
        "--env-file",
        str(ROOT / ".env.production"),
        "-f",
        str(SCRIPT / ("compose.host.yaml" if frontend == "host" else "compose.production.yaml")),
        *args,
        env=compose_env(request, frontend),
    )


def save_report(report):
    path = ROOT / "bootstrap-report.json"
    temporary = ROOT / "bootstrap-report.tmp"
    temporary.unlink(missing_ok=True)
    write_private(temporary, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def bootstrap(request):
    report = {"status": "preflight", "sha": request.get("sha", ""), "checks": []}
    nginx_changed, deployed = False, False
    command = target = backup = original = frontend = None
    # Global lock has no side effect on services/config; shared by bootstrap and deploy.
    with GLOBAL_LOCK.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Failure("Another production operation holds the server lock") from exc
        try:
            release, nginx, homepage, present, compose_present = preflight(request)
            command, target, original, proposed, frontend, container = nginx
            report.update(
                {
                    "os": release["PRETTY_NAME"],
                    "nginx": "container" if container else "host/systemd",
                    "nginx_config": str(target),
                    "existing_services": run(
                        [
                            "systemctl",
                            "list-units",
                            "--type=service",
                            "--state=running",
                            "--no-legend",
                            "--plain",
                        ]
                    ).stdout.splitlines(),
                }
            )
            report["existing_services"] = [
                line.split()[0] for line in report["existing_services"] if line.split()
            ]
            print(
                "All preliminary checks passed; beginning isolated Guide provisioning", flush=True
            )
            ROOT.mkdir(mode=0o700, exist_ok=True)
            ROOT.chmod(0o700)
            (ROOT / ".bootstrap-owner").touch(mode=0o600)
            report["stage"] = "install Docker/Compose"
            report["docker"] = install_docker(release, present, compose_present)
            report["stage"] = "initialize isolated Guide resources"
            initialize(request, frontend, container)
            registry = ROOT / (".bootstrap-registry-" + request["run_id"])
            registry.mkdir(mode=0o700)
            os.environ["DOCKER_CONFIG"] = str(registry)
            try:
                report["stage"] = "authenticate registry and verify pinned images"
                docker(
                    "login",
                    "ghcr.io",
                    "--username",
                    request["actor"],
                    "--password-stdin",
                    data=request["token"],
                )
                docker("pull", request["image"])
                revision = docker(
                    "image",
                    "inspect",
                    "--format",
                    '{{index .Config.Labels "org.opencontainers.image.revision"}}',
                    request["image"],
                ).stdout.strip()
                if revision != request["sha"]:
                    raise Failure("Guide image revision differs from requested SHA")
                docker("pull", request["postgres_image"])
                report["stage"] = "start dedicated PostgreSQL"
                env = compose_env(request, frontend)
                docker(
                    "compose",
                    "--project-name",
                    "involta-guide-data",
                    "--env-file",
                    str(ROOT / ".env.database"),
                    "-f",
                    str(ROOT / "compose.database.yaml"),
                    "up",
                    "-d",
                    "--wait",
                    "--wait-timeout",
                    "150",
                    "postgres",
                    env=env,
                )
                # Only a dedicated Guide volume is writable by this root helper.
                docker(
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--user",
                    "0:0",
                    "--entrypoint",
                    "chown",
                    "-v",
                    "involta-guide-audio:/data",
                    request["image"],
                    "10001:10001",
                    "/data",
                )
                report["stage"] = "migrate and seed dedicated Guide database"
                compose(
                    request,
                    frontend,
                    "run",
                    "--rm",
                    "--no-deps",
                    "web",
                    "alembic",
                    "upgrade",
                    "head",
                )
                compose(request, frontend, "run", "--rm", "--no-deps", "web", "guide", "seed")
                env["BOOTSTRAP_LOCAL_ONLY"] = "1"
                # Inner controller uses its own deployment lock; all bootstrap entry points hold this global lock.
                run(
                    [
                        "bash",
                        str(SCRIPT / "remote-release.sh"),
                        "deploy",
                        str(ROOT),
                        request["sha"],
                        request["image"],
                        request["run_id"],
                        "https://involta.ru/guide/",
                        request["actor"],
                    ],
                    data=request["token"] + "\n",
                    env=env,
                    timeout=1200,
                )
                deployed = True
                report["stage"] = "backup/test/reload nginx"
                if target.read_text() != original:
                    raise Failure(
                        "nginx configuration changed during provisioning; refusing to overwrite it"
                    )
                backup_dir = ROOT / "nginx-backups" / request["run_id"]
                backup_dir.mkdir(parents=True, mode=0o700)
                backup = backup_dir / "site.conf"
                shutil.copy2(target, backup)
                report["nginx_backup"] = str(backup)
                state = {
                    "sha": request["sha"],
                    "image": request["image"],
                    "run_id": request["run_id"],
                    "frontend": frontend,
                    "command": command,
                    "target": str(target),
                    "backup": str(backup),
                    "candidate_hash": hashlib.sha256(proposed.encode()).hexdigest(),
                    "homepage": homepage,
                }
                write_private(ROOT / "bootstrap-rollback.json", json.dumps(state))
                nginx_changed = True
                # Preserve inode for single-file bind mounts. Original mode/owner are retained.
                target.write_text(proposed)
                run([*command, "-t"])
                run([*command, "-s", "reload"])
                report["stage"] = "HTTPS and main site verification"
                for attempt in range(6):
                    try:
                        run(
                            [
                                "bash",
                                str(SCRIPT / "check-release.sh"),
                                "https://involta.ru/guide/",
                                request["sha"],
                            ]
                        )
                        if baseline() != homepage:
                            raise Failure("Existing homepage HTTP behavior changed")
                        break
                    except Failure:
                        if attempt == 5:
                            raise
                        time.sleep(2)
                report["checks"] = [
                    "nginx -t",
                    "local HTTP readiness and SHA",
                    "HTTPS readiness and SHA",
                    "directory/admin routes",
                    "CSS/HTMX/widget JavaScript",
                    "homepage status/redirect unchanged",
                ]
                report["status"] = "success"
                report["stage"] = "complete"
                report["image"] = request["image"]
                report["postgres_image"] = request["postgres_image"]
                report["admin_credentials"] = (
                    "Use prod ADMIN_PASSWORD/ADMIN_PASSWORD_HASH; otherwise generated only in protected server .env.production"
                )
                report["openai_configured"] = bool(request.get("openai_api_key"))
                report["versions"] = {
                    "docker": docker("--version").stdout.strip(),
                    "compose": docker("compose", "version", "--short").stdout.strip(),
                }
                print("Bootstrap succeeded: https://involta.ru/guide/", flush=True)
            finally:
                shutil.rmtree(registry, ignore_errors=True)
                os.environ.pop("DOCKER_CONFIG", None)
        except BaseException as exc:
            report["status"] = "failed"
            report["error"] = str(exc) if isinstance(exc, Failure) else type(exc).__name__
            rollback_errors = []
            if nginx_changed:
                try:
                    # Restore only the file changed by this transaction, with exact saved metadata.
                    target.write_bytes(backup.read_bytes())
                    shutil.copystat(backup, target)
                    run([*command, "-t"])
                    run([*command, "-s", "reload"])
                    report["nginx_rollback"] = "restored and reloaded"
                except BaseException:
                    rollback_errors.append(
                        "nginx restore/test/reload failed: operator intervention required"
                    )
            if deployed:
                try:
                    compose(request, frontend, "stop", "--timeout", "240", "web", "worker")
                    compose(request, frontend, "rm", "--force", "web", "worker")
                    (ROOT / "current-release").unlink(missing_ok=True)
                    report["container_rollback"] = (
                        "failed first release removed; database/audio retained"
                    )
                except BaseException:
                    rollback_errors.append("Guide cleanup failed: operator intervention required")
            if rollback_errors:
                report["rollback_errors"] = rollback_errors
            (ROOT / "bootstrap-rollback.json").unlink(missing_ok=True)
            raise
        finally:
            if ROOT.exists() and (ROOT / ".bootstrap-owner").exists():
                save_report(report)
            print(json.dumps(report, ensure_ascii=False), flush=True)


def rollback_bootstrap(request):
    validate_request(request)
    if os.geteuid() != 0:
        raise Failure("Rollback requires root or passwordless sudo")
    with GLOBAL_LOCK.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = json.loads((ROOT / "bootstrap-rollback.json").read_text())
        expected = [request["sha"], request["image"], request["run_id"]]
        if (ROOT / "current-release").read_text().split() != expected or [
            state["sha"],
            state["image"],
            state["run_id"],
        ] != expected:
            raise Failure("Release changed; refusing to roll back another deployment")
        target, backup = Path(state["target"]), Path(state["backup"])
        if hashlib.sha256(target.read_bytes()).hexdigest() != state["candidate_hash"]:
            raise Failure("nginx changed since bootstrap; refusing to overwrite another edit")
        target.write_bytes(backup.read_bytes())
        shutil.copystat(backup, target)
        run([*state["command"], "-t"])
        run([*state["command"], "-s", "reload"])
        if baseline() != state["homepage"]:
            raise Failure("Main site did not return to baseline after nginx rollback")
        compose(request, state["frontend"], "stop", "--timeout", "240", "web", "worker")
        compose(request, state["frontend"], "rm", "--force", "web", "worker")
        (ROOT / "current-release").unlink()
        report = {
            "status": "rolled_back",
            "sha": request["sha"],
            "reason": "External runner healthcheck failed; nginx restored; Guide containers removed; data retained",
        }
        save_report(report)
        print(json.dumps(report))


def handle_signal(number, frame):
    raise Failure(f"Interrupted by signal {number}")


def main():
    os.umask(0o077)
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, handle_signal)
    # Local Docker daemon only; never inherit an arbitrary remote Docker selector.
    for name in (
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_TLS",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
    ):
        os.environ.pop(name, None)
    try:
        request = json.load(sys.stdin)
        if request.get("action") == "bootstrap-rollback":
            rollback_bootstrap(request)
        else:
            bootstrap(request)
    except (Failure, OSError, ValueError, subprocess.SubprocessError) as exc:
        message = str(exc) if isinstance(exc, Failure) else type(exc).__name__
        print("Bootstrap stopped: " + message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
