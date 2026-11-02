"""Bootstrap transaction tests: no real server, package install, or Docker mutation."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
sys.path.insert(0, str(DEPLOY))
from nginx_config import BEGIN, END, render, select_server, servers  # noqa: E402

spec = importlib.util.spec_from_file_location("bootstrap", DEPLOY / "bootstrap-production.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)

SITE = """# unrelated configuration must survive byte-for-byte
server {
    listen 80;
    server_name involta.ru;
    return 301 https://$host$request_uri;
}
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name involta.ru www.involta.ru;
    ssl_certificate /existing/cert.pem;
    ssl_certificate_key /existing/key.pem;
    location / { proxy_pass http://main_site; }
    location /other { return 200 "braces { } # inside quotes"; }
}
server { listen 443 ssl; server_name another.example; location / { return 200; } }
"""
SHA = "a" * 40
IMAGE = "ghcr.io/involtax/involta-guide@sha256:" + "1" * 64
POSTGRES = "postgres@sha256:" + "2" * 64


def request(**changes):
    return {
        "sha": SHA,
        "image": IMAGE,
        "postgres_image": POSTGRES,
        "run_id": "10-1",
        "actor": "test-actor",
        "token": "do-not-log-registry-secret",
        **changes,
    }


def dump(text=SITE):
    return "# configuration file /etc/nginx/sites-enabled/site:\n" + text


def test_only_https_domain_block_changes_and_query_redirect_survives():
    server = select_server(dump())
    assert server.file == "/etc/nginx/sites-enabled/site"
    result = render(SITE, server, "127.0.0.1:3080")
    block = result[result.index("\n    " + BEGIN) : result.index(END) + len(END) + 1]
    assert result.replace(block, "") == SITE
    assert "return 302 /guide/$is_args$args;" in result
    assert "proxy_pass http://127.0.0.1:3080;" in result
    assert "proxy_pass http://main_site;" in result
    assert len(list(servers(server.file, result))) == 3


@pytest.mark.parametrize(
    "extra",
    [
        "server { listen 443 ssl; server_name involta.ru; }",
        "server { listen 80; server_name x; location /guide/ { return 200; } }",
    ],
)
def test_ambiguous_domain_or_unmanaged_route_is_rejected(extra):
    with pytest.raises(ValueError):
        select_server(dump(SITE + extra))


def test_container_upstream_and_repeated_bootstrap_guard():
    server = select_server(dump())
    result = render(SITE, server, "involta-guide:3000")
    assert "http://involta-guide:3000;" in result
    with pytest.raises(ValueError, match="already managed"):
        render(result, select_server(dump(result)), "involta-guide:3000")
    with pytest.raises(ValueError, match="Unsafe"):
        render(SITE, server, "evil; return 200")


@pytest.mark.parametrize(
    "changes",
    [
        {"image": "ghcr.io/involtax/involta-guide:latest"},
        {"postgres_image": "postgres:17"},
        {"sha": "main"},
        {"run_id": "../other"},
        {"admin_password": "injection\nSHELL=x"},
        {"admin_password": "short"},
    ],
)
def test_invalid_metadata_and_secret_injection_rejected(changes):
    with pytest.raises(bootstrap.Failure):
        bootstrap.validate_request(request(**changes))


def test_foreign_volume_or_container_is_never_adopted(monkeypatch):
    monkeypatch.setattr(
        bootstrap,
        "docker",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, '[{"Labels":{"other":"owner"}}]', ""),
    )
    with pytest.raises(bootstrap.Failure, match="not owned"):
        bootstrap.owned("volume", "involta-guide-audio")


@pytest.fixture
def transaction(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap.os, "geteuid", lambda: 0)
    root = tmp_path / "guide"
    config = tmp_path / "nginx.conf"
    config.write_text(SITE)
    command = ["nginx"]
    proposed = render(SITE, select_server(dump()), "127.0.0.1:3080")
    monkeypatch.setattr(bootstrap, "ROOT", root)
    monkeypatch.setattr(bootstrap, "GLOBAL_LOCK", tmp_path / "global.lock")
    monkeypatch.setattr(
        bootstrap,
        "preflight",
        lambda _: (
            {"PRETTY_NAME": "Test Linux"},
            (command, config, SITE, proposed, "involta-guide-frontend", None),
            "200 ",
            True,
            True,
        ),
    )
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if args[:3] == ["bash", str(DEPLOY / "remote-release.sh"), "deploy"]:
            (root / "current-release").write_text(f"{SHA} {IMAGE} 10-1\n")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(bootstrap, "run", run)

    def docker(*args, **kwargs):
        calls.append((["docker", *args], kwargs))
        output = SHA if args[:2] == ("image", "inspect") else "test-version"
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(bootstrap, "docker", docker)
    monkeypatch.setattr(bootstrap, "initialize", lambda *a: None)
    monkeypatch.setattr(bootstrap, "install_docker", lambda *a: "Existing retained")
    monkeypatch.setattr(bootstrap, "baseline", lambda: "200 ")
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _: None)
    return root, config, calls, run


def test_preflight_failure_never_reaches_mutations(transaction, monkeypatch):
    root, config, calls, _ = transaction

    def fail(_):
        raise bootstrap.Failure("Insufficient sudo privileges")

    monkeypatch.setattr(bootstrap, "preflight", fail)
    with pytest.raises(bootstrap.Failure):
        bootstrap.bootstrap(request())
    assert not root.exists()
    assert config.read_text() == SITE
    assert not calls


def test_success_backs_up_nginx_before_test_reload_and_keeps_secrets_private(transaction, capsys):
    root, config, calls, _ = transaction
    bootstrap.bootstrap(request(openai_api_key="do-not-log-openai-secret"))
    assert (root / "nginx-backups/10-1/site.conf").read_text() == SITE
    assert BEGIN in config.read_text()
    nginx = [c for c, _ in calls if c[0] == "nginx"]
    assert nginx == [["nginx", "-t"], ["nginx", "-s", "reload"]]
    assert (root / "bootstrap-report.json").stat().st_mode & 0o077 == 0
    output = capsys.readouterr()
    assert "do-not-log" not in output.out + output.err
    report = json.loads((root / "bootstrap-report.json").read_text())
    assert report["status"] == "success"
    assert not list(root.glob(".bootstrap-registry-*"))


@pytest.mark.parametrize("failure", ["nginx-test", "nginx-reload", "public", "homepage"])
def test_post_install_failure_restores_nginx_and_only_removes_guide(
    transaction, monkeypatch, failure
):
    root, config, calls, original_run = transaction
    failed = False

    def run(args, **kwargs):
        nonlocal failed
        result = original_run(args, **kwargs)
        if not failed and (
            (failure == "nginx-test" and args == ["nginx", "-t"])
            or (failure == "nginx-reload" and args == ["nginx", "-s", "reload"])
        ):
            failed = True
            raise bootstrap.Failure("Injected nginx failure")
        if failure == "public" and args[:2] == ["bash", str(DEPLOY / "check-release.sh")]:
            raise bootstrap.Failure("Injected HTTPS failure")
        return result

    monkeypatch.setattr(bootstrap, "run", run)
    if failure == "homepage":
        monkeypatch.setattr(bootstrap, "baseline", lambda: "503 ")
    with pytest.raises(bootstrap.Failure):
        bootstrap.bootstrap(request())
    assert config.read_text() == SITE
    assert not (root / "current-release").exists()
    commands = [c for c, _ in calls]
    removed = [c for c in commands if "rm" in c]
    assert len(removed) == 1 and removed[0][-2:] == ["web", "worker"]
    assert not any("down" in c or "prune" in c or "restart" in c for c in commands)
    report = json.loads((root / "bootstrap-report.json").read_text())
    assert report["status"] == "failed"
    assert report["nginx_rollback"] == "restored and reloaded"


def test_concurrent_nginx_edit_is_not_overwritten(transaction, monkeypatch):
    root, config, calls, original_run = transaction
    changed = SITE + "# changed by another administrator\n"

    def run(args, **kwargs):
        result = original_run(args, **kwargs)
        if args[:3] == ["bash", str(DEPLOY / "remote-release.sh"), "deploy"]:
            config.write_text(changed)
        return result

    monkeypatch.setattr(bootstrap, "run", run)
    with pytest.raises(bootstrap.Failure, match="changed during"):
        bootstrap.bootstrap(request())
    assert config.read_text() == changed
    assert not any(c[0] == "nginx" for c, _ in calls)


def test_external_failure_can_undo_first_bootstrap(transaction):
    root, config, calls, _ = transaction
    bootstrap.bootstrap(request())
    bootstrap.rollback_bootstrap(request())
    assert config.read_text() == SITE
    assert not (root / "current-release").exists()
    assert json.loads((root / "bootstrap-report.json").read_text())["status"] == "rolled_back"


def test_stale_bootstrap_cannot_undo_newer_release(transaction):
    root, config, calls, _ = transaction
    bootstrap.bootstrap(request())
    (root / "current-release").write_text(f"{'b' * 40} {IMAGE} 11-1\n")
    with pytest.raises(bootstrap.Failure, match="Release changed"):
        bootstrap.rollback_bootstrap(request())
    assert BEGIN in config.read_text()


def test_external_nginx_edit_is_preserved_on_late_rollback(transaction):
    root, config, calls, _ = transaction
    bootstrap.bootstrap(request())
    config.write_text(config.read_text() + "# administrator change\n")
    with pytest.raises(bootstrap.Failure, match="nginx changed"):
        bootstrap.rollback_bootstrap(request())
    assert "# administrator change" in config.read_text()


def test_error_output_is_not_disclosed(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a, 1, "PRIVATE_KEY=do-not-log", "password=do-not-log"
        ),
    )
    with pytest.raises(bootstrap.Failure) as error:
        bootstrap.run(["docker", "login"])
    assert "do-not-log" not in str(error.value)


def test_installer_preserves_existing_engine_and_compose(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Working Docker must not be changed")

    monkeypatch.setattr(bootstrap, "run", unexpected)
    assert "retained" in bootstrap.install_docker({}, True, True)


@pytest.mark.parametrize("existing_engine", [False, True])
def test_official_install_does_not_restart_foreign_services_or_enable_firewall(
    tmp_path, monkeypatch, existing_engine
):
    policy, key, source, daemon = [tmp_path / n for n in ("policy", "key", "source", "daemon")]
    for name, path in (
        ("POLICY", policy),
        ("DOCKER_KEY", key),
        ("DOCKER_SOURCE", source),
        ("DOCKER_DAEMON", daemon),
    ):
        monkeypatch.setattr(bootstrap, name, path)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[0] == "apt-get":
            assert policy.read_text().endswith("exit 101\n")
            assert kwargs["env"]["NEEDRESTART_MODE"] == "l"
        if args[0] == "curl":
            assert args[-1] == "https://download.docker.com/linux/ubuntu/gpg"
            key.write_text("public key")
        output = (
            "fpr:::::::::9DC858229FC7DD38854AE2D88D81803C0EBFCD88:\n"
            if args[0] == "gpg"
            else "amd64\n"
        )
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(bootstrap, "run", run)
    bootstrap.install_docker({"ID": "ubuntu", "VERSION_CODENAME": "noble"}, existing_engine, False)
    assert not policy.exists()
    assert "https://download.docker.com/linux/ubuntu" in source.read_text()
    assert not any("restart" in c or "nginx" in c for c in calls)
    if existing_engine:
        assert not daemon.exists()
        assert not any(c[0] == "systemctl" for c in calls)
    else:
        assert json.loads(daemon.read_text()) == {
            "bridge": "none",
            "iptables": False,
            "ip6tables": False,
            "ip-forward": False,
            "ip-masq": False,
        }
        assert ["systemctl", "enable", "--now", "docker"] in calls


def test_failed_package_install_removes_temporary_service_policy(tmp_path, monkeypatch):
    policy = tmp_path / "policy"
    monkeypatch.setattr(bootstrap, "POLICY", policy)

    def fail(*args, **kwargs):
        raise bootstrap.Failure("apt-get failed")

    monkeypatch.setattr(bootstrap, "run", fail)
    with pytest.raises(bootstrap.Failure):
        bootstrap.install_docker({"ID": "ubuntu"}, False, False)
    assert not policy.exists()


def test_real_preflight_rejects_non_root_before_any_server_checks(monkeypatch):
    monkeypatch.setattr(bootstrap.os, "geteuid", lambda: 1000)

    def unexpected(*a, **k):
        pytest.fail("Must reject privileges before invoking server commands")

    monkeypatch.setattr(bootstrap, "run", unexpected)
    with pytest.raises(bootstrap.Failure, match="passwordless sudo"):
        bootstrap.preflight(request())


@pytest.mark.parametrize("unsafe", [None, "named-volume", "multiple-networks"])
def test_container_nginx_bind_mount_and_network_discovery(tmp_path, monkeypatch, unsafe):
    (tmp_path / "site").write_text(SITE)
    meta = {
        "Args": ["nginx", "-g", "daemon off;"],
        "Mounts": [
            {"Destination": "/etc/nginx/sites-enabled", "Source": str(tmp_path), "Type": "bind"}
        ],
        "HostConfig": {"NetworkMode": "site-network"},
        "NetworkSettings": {"Networks": {"site-network": {}}},
    }
    if unsafe == "named-volume":
        meta["Mounts"][0]["Type"] = "volume"
    if unsafe == "multiple-networks":
        meta["NetworkSettings"]["Networks"]["other-network"] = {}
    monkeypatch.setattr(
        bootstrap.shutil, "which", lambda name: "/bin/docker" if name == "docker" else None
    )

    def docker(*args, **kwargs):
        if args[0] == "ps":
            value = "abc123 nginx:stable main-nginx\n"
        elif args[0] == "inspect":
            value = json.dumps([meta])
        else:
            value = json.dumps([{"Driver": "bridge", "Internal": False}])
        return subprocess.CompletedProcess(args, 0, value, "")

    monkeypatch.setattr(bootstrap, "docker", docker)
    monkeypatch.setattr(
        bootstrap, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, dump(), "")
    )
    if unsafe:
        with pytest.raises(bootstrap.Failure):
            bootstrap.nginx_candidates()
    else:
        command, path, original, proposed, network, container = bootstrap.nginx_candidates()
        assert path == tmp_path / "site"
        assert original == SITE
        assert "http://involta-guide:3000" in proposed
        assert network == "site-network" and container == "abc123"
        assert command == [
            "docker",
            "--host=unix:///var/run/docker.sock",
            "exec",
            "abc123",
            "nginx",
        ]


@pytest.mark.parametrize("secret", ["x", "-", "секрет 🔑", "a'b\\c$=+&", "line one\nINJECTED=yes"])
def test_arbitrary_alex_secret_survives_production_environment(tmp_path, monkeypatch, secret):
    import base64

    from dotenv import dotenv_values

    from involta_guide.config import Settings

    bootstrap.validate_request(request(alex_secret=secret))
    root = tmp_path / "guide"
    root.mkdir()
    monkeypatch.setattr(bootstrap, "ROOT", root)
    monkeypatch.setattr(bootstrap, "owned", lambda *a: False)
    monkeypatch.setattr(
        bootstrap, "docker", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")
    )
    monkeypatch.delenv("ALEX_SECRET", raising=False)
    monkeypatch.delenv("ALEX_SECRET_BASE64", raising=False)
    bootstrap.initialize(request(alex_secret=secret), "host")
    values = dotenv_values(root / ".env.production")
    assert "INJECTED" not in values
    assert "ALEX_SECRET" not in values
    assert base64.b64decode(values["ALEX_SECRET_BASE64"]).decode("utf-8") == secret
    configured = Settings(_env_file=root / ".env.production")
    assert configured.alex_secret.get_secret_value() == secret


@pytest.mark.parametrize(
    "family,version,codename",
    [
        ("ubuntu", "20.04", "focal"),
        ("ubuntu", "25.10", "questing"),
        ("ubuntu", "26.04", "resolute"),
        ("debian", "11", "bullseye"),
        ("debian", "13", "trixie"),
    ],
)
def test_os_detection_uses_family_and_repository_not_fixed_version_list(family, version, codename):
    bootstrap.validate_os_release(
        {"ID": family, "VERSION_ID": version, "VERSION_CODENAME": codename}
    )


@pytest.mark.parametrize(
    "release",
    [
        {"ID": "fedora", "VERSION_CODENAME": "x"},
        {"ID": "ubuntu", "VERSION_CODENAME": "../noble"},
        {"ID": "debian"},
    ],
)
def test_unsupported_package_manager_or_unsafe_codename_still_stops(release):
    with pytest.raises(bootstrap.Failure):
        bootstrap.validate_os_release(release)


def test_official_repository_unavailable_fails_without_installing_packages(monkeypatch):
    calls = []

    def missing(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 22, "404", "")

    monkeypatch.setattr(bootstrap, "run", missing)
    with pytest.raises(bootstrap.Failure, match="Official Docker repository unavailable"):
        bootstrap.check_docker_repository({"ID": "ubuntu", "VERSION_CODENAME": "focal"})
    assert len(calls) == 1 and calls[0][0] == "curl"
    assert calls[0][-1] == "https://download.docker.com/linux/ubuntu/dists/focal/Release"


def external_request(**updates):
    return request(
        proxy_mode="external", http_port=3001, trusted_proxy_ips="135.106.210.137", **updates
    )


def external_preflight(_):
    return (
        {"PRETTY_NAME": "Ubuntu 26.04.1 LTS"},
        (None, None, None, None, "host", None),
        "200 ",
        True,
        True,
    )


def test_external_proxy_starts_on_3001_without_touching_nginx(transaction, monkeypatch):
    root, config, calls, _ = transaction
    monkeypatch.setattr(bootstrap, "preflight", external_preflight)
    bootstrap.bootstrap(external_request())
    assert config.read_text() == SITE
    assert not (root / "nginx-backups").exists()
    assert not any(command[0] == "nginx" for command, _ in calls)
    envs = [
        kwargs["env"]
        for command, kwargs in calls
        if command[:2] == ["docker", "compose"] and "env" in kwargs
    ]
    assert envs and all(
        env["GUIDE_HTTP_PORT"] == "3001"
        and env["GUIDE_BIND_ADDRESS"] == "0.0.0.0"
        and env["NETWORK_MODE"] == "host"
        for env in envs
    )
    state = json.loads((root / "bootstrap-rollback.json").read_text())
    assert state["proxy_mode"] == "external" and "backup" not in state
    report = json.loads((root / "bootstrap-report.json").read_text())
    assert report["nginx"] == "external (unchanged)"
    assert report["backend_listener"] == "0.0.0.0:3001"


def test_external_proxy_https_failure_removes_only_guide(transaction, monkeypatch):
    root, config, calls, original_run = transaction
    monkeypatch.setattr(bootstrap, "preflight", external_preflight)

    def fail_public(args, **kwargs):
        result = original_run(args, **kwargs)
        if args[:2] == ["bash", str(DEPLOY / "check-release.sh")]:
            raise bootstrap.Failure("External proxy is unreachable")
        return result

    monkeypatch.setattr(bootstrap, "run", fail_public)
    with pytest.raises(bootstrap.Failure, match="External proxy"):
        bootstrap.bootstrap(external_request())
    assert config.read_text() == SITE
    assert not (root / "current-release").exists()
    assert not any(command[0] == "nginx" for command, _ in calls)
    assert any("rm" in command and command[-2:] == ["web", "worker"] for command, _ in calls)


def test_external_proxy_late_rollback_does_not_require_local_nginx(transaction, monkeypatch):
    root, config, calls, _ = transaction
    monkeypatch.setattr(bootstrap, "preflight", external_preflight)
    bootstrap.bootstrap(external_request())
    bootstrap.rollback_bootstrap(external_request())
    assert config.read_text() == SITE
    assert not (root / "current-release").exists()
    assert not any(command[0] == "nginx" for command, _ in calls)


def test_external_proxy_saved_options_match_application_binding(tmp_path, monkeypatch):
    root = tmp_path / "guide"
    root.mkdir()
    monkeypatch.setattr(bootstrap, "ROOT", root)
    monkeypatch.setattr(bootstrap, "owned", lambda *a: False)
    monkeypatch.setattr(
        bootstrap, "docker", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")
    )
    bootstrap.initialize(external_request(), "host")
    options = dict(line.split("=", 1) for line in (root / "deploy.env").read_text().splitlines())
    assert options["GUIDE_HTTP_PORT"] == "3001"
    assert options["GUIDE_BIND_ADDRESS"] == "0.0.0.0"
    assert options["NETWORK_MODE"] == "host"
    from dotenv import dotenv_values

    env = dotenv_values(root / ".env.production")
    assert "127.0.0.1:15432" in env["DATABASE_URL"]
    assert env["TRUSTED_PROXY_IPS"] == "135.106.210.137"


@pytest.mark.parametrize("port", [0, 65536, "3001; command"])
def test_external_port_is_validated_before_provisioning(port):
    with pytest.raises(bootstrap.Failure, match="HTTP port"):
        bootstrap.validate_request(request(proxy_mode="external", http_port=port))


def test_lxc_uses_scoped_apparmor_compatibility(monkeypatch):
    monkeypatch.setattr(
        bootstrap, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "lxc\n", "")
    )
    assert bootstrap.detect_apparmor_profile() == "unconfined"


def test_native_apparmor_profile_is_preserved(monkeypatch):
    monkeypatch.setattr(
        bootstrap, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "none\n", "")
    )
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _: "/bin/docker")
    monkeypatch.setattr(
        bootstrap,
        "docker",
        lambda *a, **k: subprocess.CompletedProcess(
            a, 0, '["name=apparmor,profile=default","name=seccomp,profile=builtin"]', ""
        ),
    )
    assert bootstrap.detect_apparmor_profile() == "docker-default"


def test_failed_first_install_can_add_runtime_profile_without_changing_secrets(
    tmp_path, monkeypatch
):
    root = tmp_path / "guide"
    root.mkdir()
    monkeypatch.setattr(bootstrap, "ROOT", root)
    monkeypatch.setattr(bootstrap, "owned", lambda *a: False)
    monkeypatch.setattr(
        bootstrap, "docker", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")
    )
    original = external_request()
    bootstrap.initialize(original, "host")
    secrets_before = {
        name: (root / name).read_bytes() for name in (".env.database", ".env.production")
    }
    path = root / "deploy.env"
    path.write_text(
        "\n".join(
            line
            for line in path.read_text().splitlines()
            if not line.startswith("GUIDE_APPARMOR_PROFILE=")
        )
        + "\n"
    )
    bootstrap.initialize({**original, "apparmor_profile": "unconfined"}, "host")
    assert "GUIDE_APPARMOR_PROFILE=unconfined\n" in path.read_text()
    for name, before in secrets_before.items():
        assert (root / name).read_bytes() == before


@pytest.mark.parametrize(
    "cached,free_mib,success",
    [(True, 700, True), (True, 300, False), (False, 700, False), (False, 4000, True)],
)
def test_storage_reserve_accounts_for_cached_images(monkeypatch, cached, free_mib, success):
    from types import SimpleNamespace

    monkeypatch.setattr(
        bootstrap.shutil, "disk_usage", lambda _: SimpleNamespace(free=free_mib * 1024**2)
    )

    def docker(*args, **kwargs):
        return subprocess.CompletedProcess(
            args, 0 if cached else 1, "cached-image-id" if cached else "", ""
        )

    monkeypatch.setattr(bootstrap, "docker", docker)
    if success:
        bootstrap.check_storage(request(), True)
    else:
        with pytest.raises(bootstrap.Failure, match="Insufficient storage"):
            bootstrap.check_storage(request(), True)
