"""Run disjoint pytest groups in a bounded number of processes, with complete logs."""

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def test_groups(root: Path, profile: str) -> dict[str, list[str]]:
    def files(folder):
        return {str(p.relative_to(root)) for p in (root / folder).rglob("test_*.py")}

    release_controller = {"tests/integration/test_deployment.py"}
    groups = {
        "application": files("tests/unit") | (files("tests/integration") - release_controller),
        "browser": files("tests/e2e"),
        "deployment": files("tests/deployment") | release_controller,
    }
    assigned = set().union(*groups.values())
    unknown = files("tests") - assigned
    if unknown:
        raise ValueError("Assign new tests to a group: " + ", ".join(sorted(unknown)))
    if profile == "app":
        del groups["deployment"]
    return {name: sorted(paths) for name, paths in groups.items()}


def run_group(name: str, paths: list[str], output: Path) -> dict:
    started = time.monotonic()
    folder = output / name
    folder.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--strict-markers",
        "--durations=25",
        f"--junitxml={folder / 'junit.xml'}",
        f"--basetemp={folder / 'tmp'}",
        "-o",
        f"cache_dir={folder / 'cache'}",
        *paths,
    ]
    print(f"[{name}] starting {len(paths)} test files", flush=True)
    with (folder / "pytest.log").open("w") as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
    elapsed = round(time.monotonic() - started, 2)
    print(
        f"[{name}] exit={result.returncode}, elapsed={elapsed}s\n"
        + (folder / "pytest.log").read_text(),
        flush=True,
    )
    return {"group": name, "exit_code": result.returncode, "elapsed_seconds": elapsed}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("app", "full"), default="full")
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    args = parser.parse_args()
    output = Path("var/test-results").resolve()
    groups = test_groups(Path.cwd(), args.profile)
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run_group, name, paths, output) for name, paths in groups.items()]
        results = [future.result() for future in futures]
    report = {
        "profile": args.profile,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "groups": results,
    }
    (output / "summary.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)
    return int(any(result["exit_code"] != 0 for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
