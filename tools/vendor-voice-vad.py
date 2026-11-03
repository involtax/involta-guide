"""Verify pinned browser VAD assets, or restore them with --restore (network required)."""

import argparse
import base64
import hashlib
import io
import json
import tarfile
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1] / "src/involta_voice/static/vad"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args()
    manifest = json.loads((ROOT / "manifest.json").read_text())
    if args.restore:
        for package in manifest["packages"]:
            with urlopen(package["url"], timeout=60) as response:
                archive = response.read()
            digest = base64.b64encode(hashlib.sha512(archive).digest()).decode()
            if "sha512-" + digest != package["integrity"]:
                raise RuntimeError("Package integrity mismatch")
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
                for name in package["files"]:
                    stream = tar.extractfile("package/dist/" + name)
                    assert stream is not None
                    (ROOT / name).write_bytes(stream.read())
        for name, url in manifest["licenses"].items():
            with urlopen(url, timeout=30) as response:
                (ROOT / name).write_bytes(response.read())
    for name, expected in manifest["files"].items():
        path = ROOT / name
        if (
            path.stat().st_size != expected["bytes"]
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected["sha256"]
        ):
            raise RuntimeError("VAD asset mismatch: " + name)
    print(f"Verified {len(manifest['files'])} pinned VAD assets/licenses; no runtime CDN.")


if __name__ == "__main__":
    main()
