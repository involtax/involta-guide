import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("run_tests", ROOT / "deploy/run_tests.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_full_groups_cover_every_test_file_exactly_once_and_app_excludes_only_deployment():
    groups = runner.test_groups(ROOT, "full")
    listed = [p for paths in groups.values() for p in paths]
    assert len(listed) == len(set(listed))
    assert set(listed) == {str(p.relative_to(ROOT)) for p in (ROOT / "tests").rglob("test_*.py")}
    app = runner.test_groups(ROOT, "app")
    assert set(app) == {"application", "browser"}
    assert app["application"] == groups["application"]
    assert app["browser"] == groups["browser"]
    assert "tests/integration/test_deployment.py" in groups["deployment"]


def test_group_preserves_failure_exit_code_and_auditable_output(tmp_path):
    test = tmp_path / "test_failure.py"
    test.write_text("def test_failure():\n    assert False, 'deliberate-runner-probe'\n")
    result = runner.run_group("probe", [str(test)], tmp_path / "results")
    assert result["exit_code"] == 1
    assert "deliberate-runner-probe" in (tmp_path / "results/probe/pytest.log").read_text()
    assert (tmp_path / "results/probe/junit.xml").exists()
