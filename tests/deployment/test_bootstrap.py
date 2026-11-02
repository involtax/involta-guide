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
        {"alex_secret": "short"},
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
        assert command == ["docker", "exec", "abc123", "nginx"]
