import hashlib
import importlib.util
import json
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "deploy" / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


reuse = module("reuse-image")
promote = module("promote-image")
SHA = "a" * 40
TARGET = "b" * 40
TREE = "c" * 40
REPO = "involtax/involta-guide"
RUN = {
    "id": 10,
    "run_attempt": 1,
    "head_sha": SHA,
    "event": "pull_request",
    "status": "completed",
    "conclusion": "success",
    "path": ".github/workflows/ci.yml",
    "head_repository": {"full_name": REPO},
}
PROOF = {
    "version": 1,
    "run_id": 10,
    "attempt": 1,
    "head_sha": SHA,
    "sha": SHA,
    "tree": TREE,
    "image_id": "sha256:" + "d" * 64,
    "profile": "full",
}


@pytest.mark.parametrize(
    "change",
    [
        {"conclusion": "failure"},
        {"status": "in_progress"},
        {"event": "push"},
        {"head_repository": {"full_name": "fork/project"}},
        {"path": "other.yml"},
    ],
)
def test_ineligible_runs(change):
    assert not reuse.eligible({**RUN, **change}, REPO)


@pytest.mark.parametrize(
    "change",
    [
        {"attempt": 2},
        {"run_id": 11},
        {"head_sha": TARGET},
        {"tree": TARGET},
        {"image_id": "wrong"},
        {"sha": "--bad"},
        {"profile": "app"},
        {"version": 2},
    ],
)
def test_proof_must_match_run_tree_and_required_profile(change):
    assert not reuse.validate({**PROOF, **change}, RUN, TREE, "full")


def test_full_proof_covers_app_but_not_reverse():
    assert reuse.eligible(RUN, REPO)
    assert reuse.validate(PROOF, RUN, TREE, "app")
    assert reuse.validate({**PROOF, "profile": "app"}, RUN, TREE, "app")


@pytest.mark.parametrize(
    "case", ["match", "expired", "missing_image", "different_tree", "unrelated_commit"]
)
def test_selection_requires_existing_artifact_and_actual_git_tree(monkeypatch, case):
    proof = {**PROOF, "sha": TARGET}
    names = ["verification-" + TARGET, "verified-image-" + TARGET]
    if case == "missing_image":
        names.pop()

    def api(path):
        if "/git/commits/" in path:
            return {
                "sha": TARGET,
                "tree": {"sha": "e" * 40 if case == "different_tree" else TREE},
                "parents": [{"sha": "f" * 40 if case == "unrelated_commit" else SHA}],
            }
        if "artifacts" in path:
            return {"artifacts": [{"name": n, "expired": case == "expired"} for n in names]}
        return {"workflow_runs": [RUN]}

    def git(*args):
        if args[0] == "show":
            return SHA if case != "unrelated_commit" else "f" * 40
        if args == ("rev-parse", TARGET + "^{tree}") and case == "different_tree":
            return "e" * 40
        return TREE

    def command(*args):
        (Path(args[-1]) / "verification.json").write_text(json.dumps(proof))
        return ""

    monkeypatch.setattr(reuse, "api", api)
    monkeypatch.setattr(reuse, "git", git)
    monkeypatch.setattr(reuse, "command", command)
    assert reuse.select(REPO, "full")["reuse"] == ("true" if case == "match" else "false")


def archive(path):
    layer = b"unchanged filesystem bytes"
    config = {
        "os": "linux",
        "architecture": "amd64",
        "created": "2020-01-01T00:00:00Z",
        "rootfs": {"type": "layers", "diff_ids": ["sha256:" + hashlib.sha256(layer).hexdigest()]},
        "config": {
            "User": "10001",
            "Env": ["A=B"],
            "Cmd": ["guide"],
            "Labels": {"org.opencontainers.image.revision": SHA, "keep": "yes"},
        },
    }
    raw = json.dumps(config).encode()
    identity = "sha256:" + hashlib.sha256(raw).hexdigest()
    with tarfile.open(path, "w:gz") as tar:
        promote.put(tar, "config.json", raw)
        promote.put(tar, "layer.tar", layer)
        promote.put(
            tar,
            "manifest.json",
            json.dumps(
                [{"Config": "config.json", "Layers": ["layer.tar"], "RepoTags": ["source:tag"]}]
            ).encode(),
        )
    return config, identity, layer


def test_promotion_changes_only_label_preserves_layers_and_is_repeatable(tmp_path):
    source = tmp_path / "source.gz"
    config, identity, layer = archive(source)
    expected = {
        **config,
        "config": {
            **config["config"],
            "Labels": {**config["config"]["Labels"], "org.opencontainers.image.revision": TARGET},
        },
    }
    ids = []
    for n in range(2):
        output = tmp_path / f"release-{n}.tar"
        ids.append(promote.promote(source, output, "ghcr.io/" + REPO, SHA, TARGET, identity))
        with tarfile.open(output) as tar:
            manifest = json.load(tar.extractfile("manifest.json"))[0]
            assert manifest["RepoTags"] == ["ghcr.io/" + REPO + ":" + TARGET]
            assert json.load(tar.extractfile(manifest["Config"])) == expected
            assert tar.extractfile(manifest["Layers"][0]).read() == layer
    assert ids[0] == ids[1] != identity
    assert (tmp_path / "release-0.tar").read_bytes() == (tmp_path / "release-1.tar").read_bytes()
    assert (
        promote.promote(source, tmp_path / "same.tar", "ghcr.io/" + REPO, SHA, SHA, identity)
        == identity
    )


def test_wrong_archive_rejected_before_publication(tmp_path):
    source = tmp_path / "source.gz"
    archive(source)
    with pytest.raises(ValueError, match="differs from tested"):
        promote.promote(
            source, tmp_path / "bad.tar", "ghcr.io/" + REPO, SHA, TARGET, "sha256:" + "0" * 64
        )


def test_api_failure_falls_back_without_skipping_verification(monkeypatch, tmp_path):
    import subprocess

    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("TEST_PROFILE", "full")
    monkeypatch.setattr("sys.argv", ["reuse-image.py"])

    def unavailable(*args):
        raise subprocess.CalledProcessError(1, "gh")

    monkeypatch.setattr(reuse, "select", unavailable)
    reuse.main()
    assert output.read_text() == "reuse=false\n"


def test_manual_workflow_always_verifies(monkeypatch, tmp_path):
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setattr("sys.argv", ["reuse-image.py"])

    def forbidden(*args):
        pytest.fail("Manual workflow must not look up reusable results")

    monkeypatch.setattr(reuse, "select", forbidden)
    reuse.main()
    assert output.read_text() == "reuse=false\n"


def test_record_binds_merge_tree_to_run_attempt_and_pr_head(monkeypatch, tmp_path):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"head": {"sha": SHA}}}))
    for key, value in {
        "GITHUB_EVENT_PATH": str(event),
        "GITHUB_SHA": TARGET,
        "GITHUB_RUN_ID": "10",
        "GITHUB_RUN_ATTEMPT": "2",
        "TESTED_IMAGE_ID": PROOF["image_id"],
        "TEST_PROFILE": "full",
        "RUNNER_TEMP": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(reuse, "git", lambda *args: TREE if args[-1] == "HEAD^{tree}" else TARGET)
    reuse.record()
    proof = json.loads((tmp_path / "verification.json").read_text())
    assert proof == {**PROOF, "sha": TARGET, "attempt": 2}
    assert not reuse.validate([], RUN, TREE, "full")
