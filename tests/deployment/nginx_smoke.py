"""Validate generated routes with real nginx in a disposable, network-isolated container."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy"))
from nginx_config import render, select_server  # noqa: E402


def main():
    env = dict(os.environ)
    for key in (
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_TLS",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
    ):
        env.pop(key, None)
    with tempfile.TemporaryDirectory(prefix="guide-nginx-syntax-") as directory:
        root = Path(directory)
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(root / "key.pem"),
                "-out",
                str(root / "cert.pem"),
                "-subj",
                "/CN=involta.ru",
                "-days",
                "1",
            ],
            check=True,
            capture_output=True,
        )
        text = """events {}\nhttp {
    server {
        listen 443 ssl;
        server_name involta.ru;
        ssl_certificate /fixture/cert.pem;
        ssl_certificate_key /fixture/key.pem;
        location / { return 200 "main website"; }
    }
}
"""
        selected = select_server("# configuration file /fixture/nginx.conf:\n" + text)
        for upstream in ("127.0.0.1:3080", "involta-guide:3000"):
            (root / "nginx.conf").write_text(render(text, selected, upstream))
            subprocess.run(
                [
                    "docker",
                    "--host=unix:///var/run/docker.sock",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--add-host",
                    "involta-guide:127.0.0.1",
                    "--entrypoint",
                    "nginx",
                    "--mount",
                    f"type=bind,src={root},dst=/fixture,readonly",
                    "nginx:1.28-alpine",
                    "-t",
                    "-c",
                    "/fixture/nginx.conf",
                ],
                check=True,
                env=env,
            )
            print(f"Real nginx -t passed: {upstream}")


if __name__ == "__main__":
    main()
