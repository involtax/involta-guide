"""Reuse only a successful same-repository PR run with an identical Git tree."""

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

SHA = re.compile(r"[0-9a-f]{40}")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")


def command(*args: str) -> str:
    return subprocess.check_output(args, text=True, timeout=90).strip()


def api(path: str):
    return json.loads(command("gh", "api", path))


def git(*args: str) -> str:
    return command("git", *args)


def eligible(run: dict, repository: str) -> bool:
    return (
        run.get("event") == "pull_request"
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and run.get("head_repository", {}).get("full_name") == repository
        and run.get("path") == ".github/workflows/ci.yml"
    )


def validate(proof: dict, run: dict, tree: str, profile: str) -> bool:
    if not isinstance(proof, dict):
        return False
    return (
        proof.get("version") == 1
        and proof.get("run_id") == run["id"]
        and proof.get("attempt") == run["run_attempt"]
        and proof.get("head_sha") == run["head_sha"]
        and bool(SHA.fullmatch(str(proof.get("sha", ""))))
        and proof.get("tree") == tree
        and bool(IMAGE_ID.fullmatch(str(proof.get("image_id", ""))))
        and proof.get("profile") in ("app", "full")
        and (proof["profile"] == "full" or profile == "app")
    )


def select(repository: str, profile: str) -> dict:
    tree = git("rev-parse", "HEAD^{tree}")
    runs = api(
        f"repos/{repository}/actions/workflows/ci.yml/runs"
        "?event=pull_request&status=success&per_page=20"
    )["workflow_runs"]
    for run in runs:
        if not eligible(run, repository):
            continue
        try:
            artifacts = api(f"repos/{repository}/actions/runs/{run['id']}/artifacts?per_page=100")[
                "artifacts"
            ]
            names = {a["name"] for a in artifacts if not a["expired"]}
            proofs = sorted(n for n in names if re.fullmatch(r"verification-[0-9a-f]{40}", n))
            for name in proofs:
                with tempfile.TemporaryDirectory() as directory:
                    command(
                        "gh",
                        "run",
                        "download",
                        str(run["id"]),
                        "--repo",
                        repository,
                        "--name",
                        name,
                        "--dir",
                        directory,
                    )
                    proof = json.loads((Path(directory) / "verification.json").read_text())
                if not validate(proof, run, tree, profile):
                    continue
                sha = proof["sha"]
                if name != "verification-" + sha or "verified-image-" + sha not in names:
                    continue
                # Resolve the tested Git object through the authenticated API;
                # checkout intentionally does not persist credentials for git fetch.
                commit = api(f"repos/{repository}/git/commits/{sha}")
                parents = [parent["sha"] for parent in commit["parents"]]
                if sha != run["head_sha"] and run["head_sha"] not in parents:
                    continue
                if commit["sha"] != sha or commit["tree"]["sha"] != tree:
                    continue
                return {
                    "reuse": "true",
                    "source_run": str(run["id"]),
                    "source_sha": sha,
                    "image_id": proof["image_id"],
                }
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            print(f"PR run {run['id']} cannot be reused: {type(error).__name__}")
    return {"reuse": "false"}


def record() -> None:
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    proof = {
        "version": 1,
        "sha": git("rev-parse", "HEAD"),
        "tree": git("rev-parse", "HEAD^{tree}"),
        "head_sha": event.get("pull_request", {})
        .get("head", {})
        .get("sha", os.environ["GITHUB_SHA"]),
        "run_id": int(os.environ["GITHUB_RUN_ID"]),
        "attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
        "image_id": os.environ["TESTED_IMAGE_ID"],
        "profile": os.environ["TEST_PROFILE"],
    }
    Path(os.environ["RUNNER_TEMP"], "verification.json").write_text(json.dumps(proof))


def main() -> None:
    import sys

    if sys.argv[1:] == ["record"]:
        record()
        return
    result = {"reuse": "false"}
    if (
        os.environ.get("GITHUB_EVENT_NAME") == "push"
        and os.environ.get("GITHUB_REF") == "refs/heads/main"
    ):
        try:
            result = select(os.environ["GITHUB_REPOSITORY"], os.environ["TEST_PROFILE"])
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            print(f"Reuse unavailable; full CI fallback: {type(error).__name__}")
    print(json.dumps(result))
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
        for key, value in result.items():
            stream.write(f"{key}={value}\n")


if __name__ == "__main__":
    main()
