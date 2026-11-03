"""Read-only server inventory. Do not print environments, private keys or full configs."""

import json
import os
import platform
import re
import shutil
import socket
import subprocess
from pathlib import Path


def run(args, timeout=20):
    try:
        result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
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
        "cpu_count": os.cpu_count(),
        "memory_kib": {
            line.split(":")[0]: int(line.split()[1])
            for line in Path("/proc/meminfo").read_text().splitlines()
            if line.startswith(("MemTotal:", "MemAvailable:"))
        },
        "nvidia_gpu": run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"]
        )[1].strip(),
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
    report["disk_opt"] = shutil.disk_usage("/opt")._asdict()
    report["memory_cgroup"] = {
        name: path.read_text().strip()
        for name in ("memory.max", "memory.current")
        if (path := Path("/sys/fs/cgroup") / name).is_file()
    }
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
    code, virtualization, _ = run(["systemd-detect-virt", "--container"])
    report["container_virtualization"] = virtualization.strip()
    report["docker_binary"] = shutil.which("docker")
    report["containers"] = []
    if report["docker_binary"]:
        docker = ["docker", "--host=unix:///var/run/docker.sock"]
        code, metadata, _ = run(
            [
                *docker,
                "info",
                "--format",
                '{"version":{{json .ServerVersion}},"driver":{{json .Driver}},"cgroup_driver":{{json .CgroupDriver}},"cgroup_version":{{json .CgroupVersion}},"security_options":{{json .SecurityOptions}}}',
            ]
        )
        if code == 0:
            report["docker_info"] = json.loads(metadata)
        code, version, _ = run([*docker, "compose", "version", "--short"])
        report["compose_version"] = version.strip() if code == 0 else "unavailable"
        code, output, _ = run([*docker, "ps", "--all", "--quiet"])
        report["docker_access_exit"] = code
        for identifier in output.splitlines():
            template = '{"name":{{json .Name}},"image":{{json .Config.Image}},"network_mode":{{json .HostConfig.NetworkMode}},"networks":{{json .NetworkSettings.Networks}},"ports":{{json .NetworkSettings.Ports}},"mounts":{{json .Mounts}},"owner":{{json (index .Config.Labels "com.involta.guide.owner")}},"status":{{json .State.Status}},"exit_code":{{json .State.ExitCode}},"error":{{json .State.Error}}}'
            code, metadata, _ = run([*docker, "inspect", "--format", template, identifier])
            if code:
                continue
            item = json.loads(metadata)
            if item.get("owner") != "production":
                item.pop("error", None)
            if (
                item.get("owner") == "production"
                and item.get("name") == "/involta-guide-production-worker-1"
                and item.get("status") == "running"
            ):
                program = """
import json
from sqlalchemy import text
from involta_guide.config import Settings
from involta_guide.infrastructure.ai.audit import AIAudit
from involta_guide.infrastructure.persistence.unit_of_work import create_database
settings=Settings()
engine=create_database(settings)
audit=AIAudit(engine,settings)
with engine.connect() as conn:
    ids=conn.execute(text("SELECT j.id FROM jobs j WHERE j.kind='generate' AND j.status='failed' AND EXISTS(SELECT 1 FROM audit a WHERE a.action='temporary_public_fill' AND a.target=j.id::text) ORDER BY j.created_at DESC LIMIT 3")).scalars().all()
reports=[]
for job_id in ids:
    detail=audit.detail(str(job_id))
    events=[]
    for event in detail['events']:
        payload=event['payload']
        if not isinstance(payload,dict): continue
        if payload.get('event')=='response': events.append(payload)
        if payload.get('event')=='job_failed': events.append({'event':'job_failed','data':{k:v for k,v in payload.get('data',{}).items() if k in ('message','validation_errors','exception_type')}})
    reports.append({'job_id':str(job_id),'location':detail['job']['location_name'],'error':detail['job']['error'],'events':events})
print(json.dumps(audit.clean(reports),ensure_ascii=False))
engine.dispose()
"""
                probe_code, probe, _ = run([*docker, "exec", identifier, "python", "-c", program])
                if probe_code == 0:
                    report["public_generation_errors"] = json.loads(probe)
                else:
                    report["public_generation_errors"] = "Diagnostic unavailable"
            for binary in ("nginx", "/usr/local/openresty/nginx/sbin/nginx"):
                code, _, _ = run([*docker, "exec", identifier, binary, "-v"])
                if code == 0:
                    item["nginx"] = {
                        "binary": binary,
                        **nginx_facts([*docker, "exec", identifier, binary]),
                    }
                    break
            report["containers"].append(item)
    # Read-only identifiers/statuses only; never export recordings or transcript content.
    if report.get("docker_binary"):
        probe_program = """
import json
from sqlalchemy import text
from involta_guide.config import Settings
from involta_guide.infrastructure.persistence.unit_of_work import create_database
e = create_database(Settings())
with e.connect() as c:
    rows = c.execute(text("SELECT r.id,l.slug,r.status,r.transcript IS NOT NULL AS has_transcript,v.reference AS asr_reference,r.number FROM reviews r JOIN locations l ON l.id=r.location_id LEFT JOIN voice_links v ON v.review_id=r.id ORDER BY r.created_at DESC LIMIT 10")).mappings().all()
    debug = c.execute(text("SELECT target AS request_id,detail FROM audit WHERE action='voice_upload_http' ORDER BY created_at DESC LIMIT 20")).mappings().all()
print(json.dumps({'recordings':[dict(r) for r in rows], 'upload_debug':[dict(r) for r in debug]},default=str))
e.dispose()
"""
        code, output, _ = run(
            [
                "docker",
                "--host=unix:///var/run/docker.sock",
                "exec",
                "involta-guide",
                "python",
                "-c",
                probe_program,
            ]
        )
        if code == 0:
            report["recent_voice_status"] = json.loads(output)
    try:
        report["involta_ru_addresses"] = sorted(
            {item[4][0] for item in socket.getaddrinfo("involta.ru", 443)}
        )
    except OSError:
        report["involta_ru_addresses"] = []
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
