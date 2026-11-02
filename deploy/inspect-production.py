"""Read-only server inventory. Do not print environments, private keys or full configs."""

import json
import os
import platform
import re
import shutil
import socket
import subprocess
from pathlib import Path


def run(args):
    try:
        result = subprocess.run(args, text=True, capture_output=True, timeout=20)
        return result.returncode, result.stdout, result.stderr
    except (OSError, subprocess.TimeoutExpired):
        return 127, "", ""


def nginx_facts(command):
    code, output, _ = run([*command, "-T"])
    names = re.findall(r"(?m)^\s*server_name\s+([^;]+);", output)
    listens = re.findall(r"(?m)^\s*listen\s+([^;]+);", output)
    files = re.findall(r"(?m)^# configuration file ([^\n]+):", output)
    guide = [
        match.group(0).strip()
        for line in output.splitlines()
        if (match := re.match(r"\s*location\s+[^{};]+", line)) and "/guide" in match.group(0)
    ]
    return {
        "config_test_exit": code,
        "server_names": names,
        "listens": listens,
        "config_files": files,
        "guide_locations": guide,
    }


def main():
    report = {
        "uid": os.geteuid(),
        "hostname": socket.gethostname(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
    }
    report["os_release"] = {
        k: v.strip('"')
        for line in Path("/etc/os-release").read_text().splitlines()
        if "=" in line
        for k, v in [line.split("=", 1)]
    }
    code, output, _ = run(
        ["systemctl", "list-units", "--type=service", "--state=running", "--no-legend", "--plain"]
    )
    report["running_services"] = [line.split()[0] for line in output.splitlines() if line.split()]
    code, output, _ = run(["ss", "-lntp"])
    report["listeners"] = output.splitlines()
    report["host_nginx"] = []
    for binary in dict.fromkeys(
        filter(
            None,
            [
                shutil.which("nginx"),
                shutil.which("openresty"),
                "/usr/local/openresty/nginx/sbin/nginx",
            ],
        )
    ):
        if Path(binary).is_file():
            report["host_nginx"].append({"binary": binary, **nginx_facts([binary])})
    report["nginx_processes"] = []
    for directory in Path("/proc").iterdir():
        if not directory.name.isdigit():
            continue
        try:
            if (directory / "comm").read_text().strip() in ("nginx", "openresty"):
                report["nginx_processes"].append(
                    {"pid": directory.name, "executable": os.readlink(directory / "exe")}
                )
        except OSError:
            pass
    report["docker_binary"] = shutil.which("docker")
    report["containers"] = []
    if report["docker_binary"]:
        docker = ["docker", "--host=unix:///var/run/docker.sock"]
        code, output, _ = run([*docker, "ps", "--quiet"])
        report["docker_access_exit"] = code
        for identifier in output.splitlines():
            template = '{"name":{{json .Name}},"image":{{json .Config.Image}},"network_mode":{{json .HostConfig.NetworkMode}},"networks":{{json .NetworkSettings.Networks}},"ports":{{json .NetworkSettings.Ports}},"mounts":{{json .Mounts}}}'
            code, metadata, _ = run([*docker, "inspect", "--format", template, identifier])
            if code:
                continue
            item = json.loads(metadata)
            for binary in ("nginx", "/usr/local/openresty/nginx/sbin/nginx"):
                code, _, _ = run([*docker, "exec", identifier, binary, "-v"])
                if code == 0:
                    item["nginx"] = {
                        "binary": binary,
                        **nginx_facts([*docker, "exec", identifier, binary]),
                    }
                    break
            report["containers"].append(item)
    try:
        report["involta_ru_addresses"] = sorted(
            {item[4][0] for item in socket.getaddrinfo("involta.ru", 443)}
        )
    except OSError:
        report["involta_ru_addresses"] = []
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
