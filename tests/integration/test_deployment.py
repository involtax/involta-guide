"""Deployment controller tests: real bash/flock, fake Docker/curl; no production access."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
A = "a" * 40
B = "b" * 40
C = "c" * 40
IMAGES = {
    A: "ghcr.io/involtax/involta-guide@sha256:" + "1" * 64,
    B: "ghcr.io/involtax/involta-guide@sha256:" + "2" * 64,
    C: "ghcr.io/involtax/involta-guide@sha256:" + "3" * 64,
}

FAKE = r"""
import json,os,sys,time
from pathlib import Path
file=Path(os.environ['FAKE_STATE'])
state=json.loads(file.read_text())
args=sys.argv[1:]
with Path(str(file)+'.log').open('a') as out:out.write(json.dumps([Path(sys.argv[0]).name,args,os.getenv('RELEASE_SHA')])+'\n')
def save():file.write_text(json.dumps(state))
if Path(sys.argv[0]).name=='curl':
 url=args[args.index('--url')+1];sha=state.get('services',{}).get('web',{}).get('sha','')
 if url.startswith('https:') and sha==os.getenv('FAKE_FAIL_PUBLIC_SHA'):sys.exit(22)
 if url.split('?',1)[0].endswith('/health/ready'):print(json.dumps({'status':'ready','revision':sha}))
 elif '?probe=' in url:
  if url.startswith('https:') and os.getenv('FAKE_PUBLIC_REDIRECT'):
   print(os.environ['FAKE_PUBLIC_REDIRECT']+' '+os.getenv('FAKE_REDIRECT_TARGET',url.split('?',1)[0]+'/'))
  else:print(os.getenv('FAKE_LOCAL_REDIRECT','302')+' '+url.replace('?probe=','/?probe='))
 elif '--write-out' in args:
  mime='text/css' if url.endswith('.css') else 'application/javascript' if url.endswith('.js') else 'text/html'
  if url.endswith(os.getenv('FAKE_BAD_ASSET','__never__')) and sha==os.getenv('FAKE_BAD_ASSET_SHA'):mime='text/html'
  print('200 '+mime)
 sys.exit(0)
if args[0]=='login':sys.stdin.read();sys.exit(0)
if args[0]=='pull':time.sleep(float(os.getenv('FAKE_PULL_SLEEP','0')));sys.exit(1 if args[-1]==os.getenv('FAKE_PULL_FAIL_IMAGE') else 0)
if args[:2]==['volume','inspect']:sys.exit(0)
if args[:2]==['image','inspect']:print(state['labels'][args[-1]]);sys.exit(0)
if args[0]=='inspect':
 target=args[-1]
 if target in ('involta-guide','involta-guide-production-worker-1'):
  service='web' if target=='involta-guide' else 'worker'
  if not state.get('services',{}).get(service):sys.exit(1)
  if '--format' in args:print(os.getenv('FAKE_OWNER','production'))
  sys.exit(0)
 service=state.get('services',{}).get(target,{})
 print('unhealthy' if service.get('sha')==os.getenv('FAKE_FAIL_SHA') else 'healthy')
 sys.exit(0)
if args[0]=='compose':
 command=next(x for x in args if x in ('up','ps','stop','rm','logs'))
 if command=='up':
  services=[s for s in ('web','worker') if s in args]
  for service in services:state.setdefault('services',{})[service]={'sha':os.environ['RELEASE_SHA'],'image':os.environ['IMAGE_REF']}
  save()
 elif command=='ps':print(args[-1])
 elif command=='rm':state['services']={};save()
 sys.exit(0)
raise SystemExit('Unexpected fake Docker invocation: '+str(args))
"""


def harness(tmp_path):
    root = tmp_path / "production"
    root.mkdir()
    (root / ".env.production").write_text("PRIVATE_CONFIGURATION=unchanged\n")
    (root / "deploy.env").write_text(
        "AUDIO_VOLUME=involta-guide_audio\nGUIDE_HTTP_PORT=3000\nBASE_PATH=/guide\nNETWORK_MODE=host\n"
    )
    state = tmp_path / "docker-state.json"
    state.write_text(
        json.dumps({"labels": {image: sha for sha, image in IMAGES.items()}, "services": {}})
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("docker", "curl"):
        path = bin_dir / tool
        path.write_text("#!" + sys.executable + "\n" + FAKE)
        path.chmod(0o700)
    env = {
        **os.environ,
        "PATH": str(bin_dir) + ":" + os.environ["PATH"],
        "FAKE_STATE": str(state),
        "GUIDE_GLOBAL_LOCK": str(tmp_path / "global.lock"),
    }
    return root, state, env


def deploy(root, env, sha, run="1-1", mode="deploy"):
    return subprocess.run(
        [
            "bash",
            str(ROOT / "deploy/remote-release.sh"),
            mode,
            str(root),
            sha,
            IMAGES[sha],
            run,
            "https://guide.example/guide/",
            "test-actor",
        ],
        input="test-only-registry-password\n",
        text=True,
        capture_output=True,
        env=env,
        timeout=15,
    )


def test_success_pins_digest_and_cleans_credentials(tmp_path):
    root, state, env = harness(tmp_path)
    result = deploy(root, env, A)
    assert result.returncode == 0, result.stderr
    assert (root / "current-release").read_text().split() == [A, IMAGES[A], "1-1"]
    assert not list(root.glob(".registry.*"))
    assert "test-only-registry-password" not in result.stdout + result.stderr
    assert json.loads(state.read_text())["services"]["worker"]["sha"] == A
    assert (root / ".env.production").read_text() == "PRIVATE_CONFIGURATION=unchanged\n"


def test_failed_update_restores_previous_release_without_deleting_data(tmp_path):
    root, state, env = harness(tmp_path)
    assert deploy(root, env, A).returncode == 0
    result = deploy(root, {**env, "FAKE_FAIL_SHA": B}, B, "2-1")
    assert result.returncode != 0
    assert "Rollback to " + A + " succeeded" in result.stderr
    assert (root / "current-release").read_text().split()[0] == A
    assert json.loads(state.read_text())["services"]["web"]["image"] == IMAGES[A]
    commands = Path(str(state) + ".log").read_text()
    assert "--volumes" not in commands and "prune" not in commands
    assert not list(root.glob(".registry.*"))


def test_external_rollback_and_old_run_guard(tmp_path):
    root, state, env = harness(tmp_path)
    assert deploy(root, env, A).returncode == 0
    assert deploy(root, env, B, "2-1").returncode == 0
    assert deploy(root, env, B, "2-1", "rollback").returncode == 0
    assert (root / "current-release").read_text().split()[0] == A
    assert deploy(root, env, C, "3-1").returncode == 0
    old = deploy(root, env, B, "2-1", "rollback")
    assert old.returncode != 0 and "refusing" in old.stderr
    assert (root / "current-release").read_text().split()[0] == C


def test_first_install_failure_stops_only_its_containers(tmp_path):
    root, state, env = harness(tmp_path)
    result = deploy(root, {**env, "FAKE_FAIL_SHA": A}, A)
    assert result.returncode != 0
    assert not (root / "current-release").exists()
    assert not json.loads(state.read_text())["services"]
    assert (root / ".env.production").exists()


def test_server_lock_serializes_deployments(tmp_path):
    root, state, env = harness(tmp_path)
    env = {**env, "FAKE_PULL_SLEEP": ".5"}
    first = subprocess.Popen(
        [
            "bash",
            str(ROOT / "deploy/remote-release.sh"),
            "deploy",
            str(root),
            A,
            IMAGES[A],
            "1-1",
            "https://guide.example/guide/",
            "test-actor",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    first.stdin.write("test-only-registry-password\n")
    first.stdin.close()
    first.stdin = None
    # Wait until the first process actually acquired the server-side lock.
    import time

    for _ in range(100):
        if Path(str(state) + ".log").exists():
            break
        time.sleep(0.01)
    second = deploy(root, env, B, "2-1")
    output, error = first.communicate(timeout=15)
    assert first.returncode == 0, error
    assert second.returncode == 0, second.stderr
    assert (root / "previous-release").read_text().split()[0] == A
    assert (root / "current-release").read_text().split()[0] == B


def test_rejects_floating_image_tags(tmp_path):
    root, state, env = harness(tmp_path)
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "deploy/remote-release.sh"),
            "deploy",
            str(root),
            A,
            "ghcr.io/involtax/involta-guide:latest",
            "1-1",
            "https://guide.example/guide/",
            "actor",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 2
    assert not (root / "current-release").exists()


def test_public_check_failure_also_rolls_back(tmp_path):
    root, state, env = harness(tmp_path)
    assert deploy(root, env, A).returncode == 0
    result = deploy(root, {**env, "FAKE_FAIL_PUBLIC_SHA": B}, B, "2-1")
    assert result.returncode != 0
    assert (root / "current-release").read_text().split()[0] == A
    assert json.loads(state.read_text())["services"]["worker"]["sha"] == A


def test_corrupted_rollback_state_is_rejected_before_changes(tmp_path):
    root, state, env = harness(tmp_path)
    assert deploy(root, env, A).returncode == 0
    (root / "current-release").write_text(f"{A} ghcr.io/involtax/involta-guide:latest 1-1\n")
    result = deploy(root, env, B, "2-1")
    assert result.returncode != 0 and "Invalid current release" in result.stderr
    assert json.loads(state.read_text())["services"]["web"]["sha"] == A


def test_foreign_container_collision_is_not_recreated(tmp_path):
    root, state, env = harness(tmp_path)
    assert deploy(root, env, A).returncode == 0
    result = deploy(root, {**env, "FAKE_OWNER": "another-project"}, B, "2-1")
    assert result.returncode != 0 and "Foreign container" in result.stderr
    assert (root / "current-release").read_text().split()[0] == A
    assert json.loads(state.read_text())["services"]["web"]["sha"] == A


def test_wrong_asset_response_triggers_rollback(tmp_path):
    root, state, env = harness(tmp_path)
    assert deploy(root, env, A).returncode == 0
    result = deploy(root, {**env, "FAKE_BAD_ASSET": "style.css", "FAKE_BAD_ASSET_SHA": B}, B, "2-1")
    assert result.returncode != 0
    assert json.loads(state.read_text())["services"]["web"]["sha"] == A


def test_rollback_uses_cached_previous_image_if_registry_is_unavailable(tmp_path):
    root, state, env = harness(tmp_path)
    assert deploy(root, env, A).returncode == 0
    result = deploy(root, {**env, "FAKE_FAIL_SHA": B, "FAKE_PULL_FAIL_IMAGE": IMAGES[A]}, B, "2-1")
    assert result.returncode != 0
    assert "Rollback to " + A + " succeeded" in result.stderr
    assert (root / "current-release").read_text().split()[0] == A


def test_external_nginx_301_does_not_undo_healthy_backend(tmp_path):
    root, state, env = harness(tmp_path)
    with (root / "deploy.env").open("a") as out:
        out.write("GUIDE_BIND_ADDRESS=0.0.0.0\n")
    result = deploy(root, {**env, "FAKE_PUBLIC_REDIRECT": "301"}, A)
    assert result.returncode == 0, result.stderr
    assert (root / "current-release").read_text().split()[0] == A
    assert "External nginx redirect: 301 https://guide.example/guide/" in result.stdout


def test_external_redirect_to_another_origin_is_rejected(tmp_path):
    root, state, env = harness(tmp_path)
    with (root / "deploy.env").open("a") as out:
        out.write("GUIDE_BIND_ADDRESS=0.0.0.0\n")
    result = deploy(
        root,
        {
            **env,
            "FAKE_PUBLIC_REDIRECT": "301",
            "FAKE_REDIRECT_TARGET": "https://wrong.example/guide/",
        },
        A,
    )
    assert result.returncode != 0
    assert not (root / "current-release").exists()


def test_backend_redirect_still_requires_302_and_query(tmp_path):
    root, state, env = harness(tmp_path)
    assert deploy(root, env, A).returncode == 0
    result = subprocess.run(
        ["bash", str(ROOT / "deploy/check-release.sh"), "http://127.0.0.1:3001/guide/", A],
        env={**env, "FAKE_LOCAL_REDIRECT": "301"},
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert "Expected backend /guide" in result.stderr
