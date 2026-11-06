"""Choose extra release-controller checks; uncertainty always means the full suite."""

import json
import os
import re
import subprocess
from pathlib import Path

FULL_PREFIXES = (
    ".github/",
    "deploy/",
    "migrations/",
    "tests/deployment/",
    "src/involta_guide/infrastructure/persistence/",
    "src/involta_guide/infrastructure/voice/",
    "src/involta_voice/migrations/",
)
FULL_FILES = {
    "Dockerfile",
    ".dockerignore",
    "pyproject.toml",
    "uv.lock",
    "alembic.ini",
    ".env.example",
    ".env.voice.example",
    "tests/conftest.py",
    "tests/integration/test_deployment.py",
    "tests/unit/test_ci_plan.py",
    "tests/unit/test_test_runner.py",
    "tests/integration/test_test_data.py",
    "src/involta_guide/config.py",
    "src/involta_guide/main.py",
    "src/involta_guide/cli.py",
    "src/involta_guide/infrastructure/jobs/worker.py",
    *(
        f"src/involta_voice/{name}.py"
        for name in (
            "config",
            "main",
            "cli",
            "db",
            "worker",
            "service",
            "storage",
            "reset",
        )
    ),
}
APP_PREFIXES = (
    "src/involta_guide/",
    "src/involta_voice/",
    "src/involta_stats/",
    "tests/unit/",
    "tests/integration/",
    "tests/e2e/",
    "data/",
    "docs/",
)
APP_FILES = {"README.md", "AGENTS.md", ".gitignore"}


def profile_for(paths: list[str] | None) -> str:
    if not paths:
        return "full"
    for path in paths:
        if path in FULL_FILES or path.startswith(FULL_PREFIXES):
            return "full"
        if path not in APP_FILES and not path.startswith(APP_PREFIXES):
            return "full"
    return "app"


def changed_paths(base: str | None, head: str, root: Path) -> list[str] | None:
    if not all(
        isinstance(s, str) and re.fullmatch(r"[0-9a-f]{40}", s) and s != "0" * 40
        for s in (base, head)
    ):
        return None
    try:
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", base, head],
            cwd=root,
            check=True,
            capture_output=True,
        )
        result = subprocess.run(
            ["git", "diff", "--name-only", "--no-renames", "-z", f"{base}..{head}"],
            cwd=root,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        return None
    return [p.decode("utf-8", errors="replace") for p in result.stdout.split(b"\0") if p]


def latest_verified_main(repository: str) -> str | None:
    try:
        response = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{repository}/actions/workflows/ci.yml/runs"
                "?branch=main&event=push&status=success&per_page=1",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        runs = json.loads(response.stdout)["workflow_runs"]
        return runs[0]["head_sha"] if runs else None
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError, TypeError):
        return None


def main() -> None:
    base = latest_verified_main(os.environ.get("GITHUB_REPOSITORY", ""))
    paths = changed_paths(base, os.environ.get("GITHUB_SHA", ""), Path.cwd())
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        paths = None
    profile = profile_for(paths)
    print(
        json.dumps(
            {"profile": profile, "verified_base": base, "changed_paths": paths}, ensure_ascii=False
        )
    )
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"profile={profile}\n")


if __name__ == "__main__":
    main()
