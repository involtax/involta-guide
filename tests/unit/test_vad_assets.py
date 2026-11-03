import hashlib
import json
from pathlib import Path


def test_vad_runtime_model_and_licenses_match_pinned_distribution():
    root = Path(__file__).parents[2] / "src/involta_voice/static/vad"
    manifest = json.loads((root / "manifest.json").read_text())
    for name, expected in manifest["files"].items():
        content = (root / name).read_bytes()
        assert len(content) == expected["bytes"]
        assert hashlib.sha256(content).hexdigest() == expected["sha256"]
    assert (root / "ort-wasm-simd-threaded.wasm").read_bytes().startswith(b"\0asm")
