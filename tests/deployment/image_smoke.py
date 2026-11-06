"""Verify the installed image, including every tracked package file and frontend asset."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

CHECK = """
import hashlib, importlib, importlib.metadata, importlib.util, json, os, shutil, subprocess, sys
from pathlib import Path
assert os.getuid() == 10001
assert shutil.which("uv") is None and shutil.which("gcc") is None
for tool in ("pytest", "playwright", "mypy", "ruff"):
    assert importlib.util.find_spec(tool) is None, f"Test tooling leaked into production: {tool}"
expected = json.load(sys.stdin)
for relative, digest in expected.items():
    package, name = relative.split("/", 1)
    root = Path(importlib.import_module(package).__file__).parent
    path = root / name
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, relative
    assert not os.access(path, os.W_OK), relative
distribution = importlib.metadata.distribution("involta-guide")
entries = {ep.name: ep for ep in distribution.entry_points}
for name in ("guide", "guide-worker", "voice", "voice-worker"):
    assert shutil.which(name), name
    assert callable(entries[name].load()), name
for name in ("guide", "voice"):
    subprocess.run([name, "--help"], check=True, stdout=subprocess.DEVNULL)
import sherpa_onnx
provenance = json.loads(importlib.metadata.distribution("sherpa-onnx").read_text("direct_url.json"))
assert provenance["vcs_info"]["commit_id"] == "3898de1380029e96a3b0a3df9e21e3f5ee5eb7fb"
from involta_voice.sherpa_tokenizer import tokenizer
assert tokenizer().vocab_size() == 1024
from involta_voice.text import catalogue_forms
assert catalogue_forms(["Иваново"], city="Иваново")["иванове"] == "Иванове"
print(f"Image verified: nonroot, CLI, metadata, native ASR, {len(expected)} exact source/assets files")
"""


def main() -> None:
    files = subprocess.check_output(["git", "ls-files", "-z", "src"]).decode().split("\0")
    expected = {
        str(Path(name).relative_to("src")): hashlib.sha256(Path(name).read_bytes()).hexdigest()
        for name in files
        if name
    }
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_TLS",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
        }
    }
    subprocess.run(
        [
            "docker",
            "--host=unix:///var/run/docker.sock",
            "run",
            "--rm",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "-i",
            os.getenv("GUIDE_TEST_IMAGE", "involta-guide:ci"),
            "python",
            "-c",
            CHECK,
        ],
        input=json.dumps(expected),
        text=True,
        env=env,
        check=True,
        timeout=90,
    )


if __name__ == "__main__":
    main()
