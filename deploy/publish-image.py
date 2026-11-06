"""Publish the already-tested Docker image; never build or overwrite a different SHA image."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def docker(*args: str) -> str:
    return subprocess.check_output(["docker", *args], text=True, timeout=60).strip()


def remote(image: str, sha: str) -> tuple[str, str] | None:
    reference = image + ":" + sha
    result = subprocess.run(
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            reference,
            "--format",
            "{{json .Manifest.Digest}}",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        if reference + ": not found" in result.stderr or "manifest unknown" in result.stderr:
            return None
        raise RuntimeError("Registry inspection failed: " + result.stderr.strip())
    try:
        digest = json.loads(result.stdout)
    except ValueError as error:
        raise RuntimeError("Registry returned an invalid manifest digest") from error
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        raise RuntimeError("Registry returned an invalid manifest digest")
    manifest = json.loads(docker("buildx", "imagetools", "inspect", image + "@" + digest, "--raw"))
    if "manifests" in manifest:
        candidates = [
            entry
            for entry in manifest["manifests"]
            if entry.get("platform", {}).get("os") == "linux"
            and entry.get("platform", {}).get("architecture") == "amd64"
        ]
        if len(candidates) != 1 or not DIGEST.fullmatch(candidates[0]["digest"]):
            raise RuntimeError("Expected one tested linux/amd64 manifest")
        manifest = json.loads(
            docker(
                "buildx", "imagetools", "inspect", image + "@" + candidates[0]["digest"], "--raw"
            )
        )
    config = manifest["config"]["digest"]
    if not DIGEST.fullmatch(config):
        raise RuntimeError("Registry returned an invalid image config digest")
    return digest, config


def publish(image: str, sha: str, expected_id: str) -> str:
    if not re.fullmatch(r"ghcr\.io/[a-z0-9_./-]+", image):
        raise ValueError("Expected a GHCR repository")
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or not DIGEST.fullmatch(expected_id):
        raise ValueError("Expected full commit SHA and tested image ID")
    reference = image + ":" + sha
    inspected = json.loads(docker("image", "inspect", reference))[0]
    if (
        inspected["Id"] != expected_id
        or inspected["Config"].get("Labels", {}).get("org.opencontainers.image.revision") != sha
        or inspected["Os"] != "linux"
        or inspected["Architecture"] != "amd64"
    ):
        raise RuntimeError("Loaded image does not match the tested ID, revision or platform")
    existing = remote(image, sha)
    if existing:
        if existing[1] != expected_id:
            raise RuntimeError("Refusing to overwrite immutable SHA tag with a different image")
        return existing[0]
    subprocess.run(["docker", "push", reference], check=True, timeout=600)
    published = remote(image, sha)
    if not published or published[1] != expected_id:
        raise RuntimeError("Published image config does not match the tested image ID")
    return published[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("sha")
    parser.add_argument("image_id")
    args = parser.parse_args()
    digest = publish(args.image, args.sha, args.image_id)
    if output := os.getenv("GITHUB_OUTPUT"):
        with Path(output).open("a") as stream:
            stream.write("digest=" + digest + "\n")
    print(f"Verified image {args.image_id}: {args.image}@{digest}")


if __name__ == "__main__":
    main()
