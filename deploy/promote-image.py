"""Change only the revision label in a verified Docker archive, deterministically.

No Dockerfile, dependency installation or filesystem layer is rebuilt. The source
config digest must match the image ID recorded by the successful test run.
"""

import argparse
import hashlib
import io
import json
import os
import re
import tarfile
from pathlib import Path


def put(archive: tarfile.TarFile, name: str, data: bytes) -> None:
    member = tarfile.TarInfo(name)
    member.size = len(data)
    member.mode = 0o644
    archive.addfile(member, io.BytesIO(data))


def promote(
    source: Path, output: Path, image: str, source_sha: str, sha: str, expected_id: str
) -> str:
    if not re.fullmatch(r"ghcr\.io/[a-z0-9_./-]+", image):
        raise ValueError("Expected GHCR repository")
    if not all(re.fullmatch(r"[0-9a-f]{40}", s) for s in (source_sha, sha)):
        raise ValueError("Expected full commit SHAs")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_id):
        raise ValueError("Expected tested config digest")
    with tarfile.open(source, "r:*") as incoming:
        manifest = json.load(incoming.extractfile("manifest.json"))
        if len(manifest) != 1:
            raise ValueError("Expected exactly one image")
        entry = manifest[0]
        raw = incoming.extractfile(entry["Config"]).read()
        if "sha256:" + hashlib.sha256(raw).hexdigest() != expected_id:
            raise ValueError("Archive config differs from tested image ID")
        config = json.loads(raw)
        if (
            config.get("os") != "linux"
            or config.get("architecture") != "amd64"
            or config["config"]["Labels"].get("org.opencontainers.image.revision") != source_sha
        ):
            raise ValueError("Unexpected source revision or platform")
        if sha != source_sha:
            config["config"]["Labels"]["org.opencontainers.image.revision"] = sha
            raw = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
        new_id = hashlib.sha256(raw).hexdigest()
        name = new_id + ".json"
        # Docker's manifest format retains every original layer, including legacy
        # layer metadata. Remove the OCI index so there is only one authoritative tag.
        with tarfile.open(output, "w") as outgoing:
            for member in incoming:
                if member.name in (
                    "manifest.json",
                    "repositories",
                    "index.json",
                    "oci-layout",
                    name,
                ):
                    continue
                outgoing.addfile(member, incoming.extractfile(member) if member.isfile() else None)
            put(outgoing, name, raw)
            put(
                outgoing,
                "manifest.json",
                json.dumps([{**entry, "Config": name, "RepoTags": [image + ":" + sha]}]).encode(),
            )
    return "sha256:" + new_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("source", "output", "image", "source_sha", "sha", "image_id"):
        parser.add_argument(arg)
    args = parser.parse_args()
    image_id = promote(
        Path(args.source), Path(args.output), args.image, args.source_sha, args.sha, args.image_id
    )
    if output := os.getenv("GITHUB_OUTPUT"):
        with Path(output).open("a") as stream:
            stream.write("image_id=" + image_id + "\n")
    print("Release image ID: " + image_id)


if __name__ == "__main__":
    main()
