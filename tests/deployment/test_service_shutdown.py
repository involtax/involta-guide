import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("legacy", [False, True])
def test_all_writers_receive_stop_before_waiting(tmp_path, legacy):
    release = tmp_path / "releases" / "test"
    release.mkdir(parents=True)
    (release / "compose.voice.yaml").touch()
    script = (ROOT / "deploy/remote-release.sh").read_text()
    helpers = script[
        script.index("voice_services() {") : script.index("restore_cutover_database() {")
    ]
    services = "voice-api voice-worker voice-consumer" if legacy else "voice voice-worker"
    expected = 5 if legacy else 4
    program = (
        """set -Eeuo pipefail
compose() {
  shift 2
  if [[ "$1" == config ]]; then printf '%s\\n' $SERVICES; return; fi
  [[ "$1" == stop && "$#" == 2 ]]
  touch "$DEPLOY_ROOT/stopped-$2"
  # Barrier: sequential stopping would deadlock; all writers must get a signal.
  while [[ $(find "$DEPLOY_ROOT" -name 'stopped-*' | wc -l) -lt "$EXPECTED" ]]; do sleep .02; done
}
"""
        + helpers
        + "\nstop_guide_writers test test-image\n"
    )
    subprocess.run(
        ["bash", "-c", program],
        env={
            **os.environ,
            "DEPLOY_ROOT": str(tmp_path),
            "SERVICES": services,
            "EXPECTED": str(expected),
        },
        check=True,
        timeout=8,
        capture_output=True,
    )
    assert {p.name for p in tmp_path.glob("stopped-*")} == {
        "stopped-" + name for name in ("web worker " + services).split()
    }


def test_previous_image_url_uses_shared_schema_without_exposing_credentials(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "shared_env", ROOT / "deploy/shared-database-env.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    release = tmp_path / "release"
    release.mkdir()
    (tmp_path / ".env.production").write_text(
        "DATABASE_URL=postgresql://guide:fake-test@localhost/guide\n"
    )
    old = "VOICE_DATABASE_URL=postgresql://voice:old-test@localhost/voice\nVOICE_ANALYSIS_MODEL=test-model\n"
    (tmp_path / ".env.voice").write_text(old)
    module.update(tmp_path, release)
    updated = (tmp_path / ".env.voice").read_text()
    assert "search_path%3Dvoice" in updated and "%20" in updated
    assert "+" not in updated and "old-test" not in updated
    assert "VOICE_ANALYSIS_MODEL=test-model" in updated
    assert (release / "voice-env-before-shared-db").read_text() == old
    assert (tmp_path / ".env.voice").stat().st_mode & 0o777 == 0o600
    module.update(tmp_path, release)
    assert (release / "voice-env-before-shared-db").read_text() == old
