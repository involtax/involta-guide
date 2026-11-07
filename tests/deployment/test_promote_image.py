import importlib.util
import json
import subprocess
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("promote_image", ROOT / "deploy/promote-image.py")
promote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(promote)


def test_real_docker_load_preserves_filesystem_and_runtime_configuration(tmp_path):
    source_sha, target_sha = "a" * 40, "b" * 40
    repository = "ghcr.io/guide-test/promotion-" + uuid4().hex
    source, target = repository + ":" + source_sha, repository + ":" + target_sha
    (tmp_path / "payload").write_text("the tested filesystem\n")
    (tmp_path / "Dockerfile").write_text(
        'FROM scratch\nCOPY payload /payload\nUSER 10001\nCMD ["/payload"]\n'
        f'LABEL org.opencontainers.image.revision="{source_sha}"\n'
    )

    def docker(*args):
        return subprocess.check_output(["docker", *args], text=True, timeout=90).strip()

    try:
        docker("build", "--network=none", "--tag", source, str(tmp_path))
        before = json.loads(docker("image", "inspect", source))[0]
        docker("image", "save", "--output", str(tmp_path / "source.tar"), source)
        expected_id = promote.promote(
            tmp_path / "source.tar",
            tmp_path / "release.tar",
            repository,
            source_sha,
            target_sha,
            before["Id"],
        )
        docker("image", "load", "--input", str(tmp_path / "release.tar"))
        after = json.loads(docker("image", "inspect", target))[0]
        assert after["Id"] == expected_id
        assert after["RootFS"] == before["RootFS"]
        before["Config"]["Labels"]["org.opencontainers.image.revision"] = target_sha
        assert after["Config"] == before["Config"]
    finally:
        subprocess.run(["docker", "image", "rm", target, source], capture_output=True, timeout=30)
