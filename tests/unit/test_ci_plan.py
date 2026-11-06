import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("ci_plan", ROOT / "deploy/ci_plan.py")
plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plan)


@pytest.mark.parametrize(
    "paths,expected",
    [
        (["src/involta_guide/application/services/locations.py"], "app"),
        (
            [
                "src/involta_guide/web/templates/directory.html",
                "tests/e2e/test_home_review_feed.py",
            ],
            "app",
        ),
        (["src/involta_voice/text.py"], "app"),
        (["src/involta_guide/config.py"], "full"),
        (["src/involta_voice/worker.py"], "full"),
        (["src/involta_guide/infrastructure/persistence/release_backup.py"], "full"),
        (["src/involta_voice/migrations/011.sql"], "full"),
        (["deploy/remote-release.sh", "docs/README.md"], "full"),
        (["Dockerfile"], "full"),
        (["uv.lock"], "full"),
        ([".github/workflows/ci.yml"], "full"),
        (["migrations/versions/new.py"], "full"),
        (["tests/conftest.py"], "full"),
        (["new-build-system.toml"], "full"),
        ([], "full"),
        (None, "full"),
    ],
)
def test_changes_select_safe_profile(paths, expected):
    assert plan.profile_for(paths) == expected


def test_diff_includes_every_commit_and_both_sides_of_a_rename(tmp_path):
    def git(*args):
        return subprocess.check_output(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
            cwd=tmp_path,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()

    git("init")
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    git("add", ".")
    git("commit", "-m", "base")
    base = git("rev-parse", "HEAD")
    (tmp_path / "docs").mkdir()
    git("mv", "Dockerfile", "docs/old-build.txt")
    git("commit", "-m", "move build")
    (tmp_path / "README.md").write_text("docs\n")
    git("add", ".")
    git("commit", "-m", "docs only in last commit")
    head = git("rev-parse", "HEAD")
    changes = plan.changed_paths(base, head, tmp_path)
    assert set(changes) == {"Dockerfile", "docs/old-build.txt", "README.md"}
    assert plan.profile_for(changes) == "full"
    assert plan.changed_paths("0" * 40, head, tmp_path) is None
    assert plan.changed_paths("f" * 40, head, tmp_path) is None


def test_unavailable_verified_baseline_uses_full_profile(monkeypatch):
    def denied(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(plan.subprocess, "run", denied)
    assert plan.latest_verified_main("owner/repo") is None
    assert plan.profile_for(plan.changed_paths(None, "a" * 40, ROOT)) == "full"
