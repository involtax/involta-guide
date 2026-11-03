#!/usr/bin/env bash
set +x
set -euo pipefail
umask 077
mode=${1:-deploy}
: "${DEPLOY_SSH_HOST:?Set repository secret DEPLOY_HOST}"
: "${DEPLOY_SSH_USER:?Set repository secret DEPLOY_USER}"
: "${DEPLOY_SSH_KEY:?Set repository secret DEPLOY_SSH_KEY}"
DEPLOY_SSH_KNOWN_HOSTS=${DEPLOY_SSH_KNOWN_HOSTS:-}
: "${GITHUB_RUN_ID:?}"
if [[ "$mode" != inspect && "$mode" != admin-password ]]; then
  : "${TARGET_SHA:?}"
  if [[ "$mode" != voice-* ]]; then : "${IMAGE_REF:?}" "${GHCR_TOKEN:?}" "${GITHUB_ACTOR:?}"; fi
  [[ "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]] || exit 2
fi
DEPLOY_SSH_PORT=${DEPLOY_SSH_PORT:-22}; DEPLOY_ROOT=${DEPLOY_ROOT:-/opt/involta-guide}
PUBLIC_URL=${PUBLIC_URL:-https://involta.ru/guide/}
[[ "$mode" == deploy || "$mode" == beta-transition || "$mode" == rollback || "$mode" == bootstrap || "$mode" == bootstrap-rollback || "$mode" == inspect || "$mode" == admin-password || "$mode" == voice-prepare || "$mode" == voice-activate || "$mode" == voice-backfill || "$mode" == voice-report ]] || exit 2
[[ "$DEPLOY_SSH_HOST" =~ ^[a-zA-Z0-9.-]+$ && "$DEPLOY_SSH_USER" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ && "$DEPLOY_SSH_PORT" =~ ^[0-9]{1,5}$ ]] || exit 2
(( DEPLOY_SSH_PORT > 0 && DEPLOY_SSH_PORT < 65536 )) || exit 2
[[ "$DEPLOY_ROOT" =~ ^/([a-zA-Z0-9_-]+/)+[a-zA-Z0-9_-]+$ ]] || exit 2
[[ "$GITHUB_RUN_ID" =~ ^[0-9]+$ && "${GITHUB_RUN_ATTEMPT:-1}" =~ ^[0-9]+$ ]] || exit 2
work=$(mktemp -d)
remote_dir="/tmp/involta-guide-release-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT:-1}"
remote="${DEPLOY_SSH_USER}@${DEPLOY_SSH_HOST}"
printf '%s\n' "$DEPLOY_SSH_KEY" | tr -d '\r' > "$work/key"
opts=(-i "$work/key" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=3)
if [[ -n "$DEPLOY_SSH_KNOWN_HOSTS" && "$DEPLOY_SSH_KNOWN_HOSTS" != - ]]; then
  printf '%s\n' "$DEPLOY_SSH_KNOWN_HOSTS" > "$work/known_hosts"
  opts+=(-o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$work/known_hosts" -o GlobalKnownHostsFile=/dev/null)
  echo 'SSH host-key verification: enabled (DEPLOY_KNOWN_HOSTS)' >&2
else
  opts+=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o GlobalKnownHostsFile=/dev/null)
  echo 'SSH host-key verification: temporarily disabled' >&2
fi
unset DEPLOY_SSH_KEY DEPLOY_SSH_KNOWN_HOSTS
remote_created=0
cleanup() {
  if [[ "$remote_created" == 1 ]]; then ssh "${opts[@]}" -p "$DEPLOY_SSH_PORT" "$remote" "rm -rf -- '$remote_dir'" </dev/null >/dev/null 2>&1 || true; fi
  rm -rf -- "$work"
}
trap cleanup EXIT
if [[ "$mode" == inspect ]]; then
  ssh "${opts[@]}" -p "$DEPLOY_SSH_PORT" "$remote" 'python3 -' < deploy/inspect-production.py
  exit 0
fi
ssh "${opts[@]}" -p "$DEPLOY_SSH_PORT" "$remote" "umask 077; mkdir '$remote_dir'"
remote_created=1
scp "${opts[@]}" -P "$DEPLOY_SSH_PORT" deploy/remote-release.sh deploy/check-release.sh deploy/compose.production.yaml deploy/compose.database.yaml deploy/compose.host.yaml deploy/compose.voice.yaml deploy/compose.voice.host.yaml deploy/compose.database-host.yaml deploy/bootstrap-production.py deploy/nginx_config.py deploy/sync-admin-password.py deploy/pause-background.py deploy/voice-rollout.py deploy/beta-transition.py "$remote:$remote_dir/"
# Check privilege before executing any production tooling. No password prompts.
privilege=$(ssh "${opts[@]}" -p "$DEPLOY_SSH_PORT" "$remote" 'if [ "$(id -u)" = 0 ]; then echo root; elif command -v sudo >/dev/null && sudo -n true; then echo sudo; else echo "Root or passwordless sudo is required" >&2; exit 1; fi')
prefix=''
[[ "$privilege" == root ]] || { [[ "$privilege" == sudo ]] || exit 1; prefix='sudo -n '; }
if [[ "$mode" == beta-transition ]]; then
  export TARGET_SHA IMAGE_REF GHCR_TOKEN GITHUB_ACTOR
  export BETA_RUN_ID="${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT:-1}"
  python3 - <<'BETA_PAYLOAD' | ssh "${opts[@]}" -p "$DEPLOY_SSH_PORT" "$remote" "${prefix}python3 -B '$remote_dir/beta-transition.py'"
import json, os
print(json.dumps({"sha": os.environ["TARGET_SHA"], "image": os.environ["IMAGE_REF"],
                  "token": os.environ["GHCR_TOKEN"], "actor": os.environ["GITHUB_ACTOR"],
                  "run_id": os.environ["BETA_RUN_ID"]}))
BETA_PAYLOAD
  exit 0
fi
if [[ "$mode" == voice-* ]]; then
  printf -v arguments '%q ' "${mode#voice-}" "$TARGET_SHA"
  ssh "${opts[@]}" -p "$DEPLOY_SSH_PORT" "$remote" "${prefix}python3 -B '$remote_dir/voice-rollout.py' $arguments" </dev/null
  exit 0
fi
if [[ "$mode" == admin-password ]]; then
  python3 - <<'PASSWORD' | ssh "${opts[@]}" -p "$DEPLOY_SSH_PORT" "$remote" "${prefix}python3 -B '$remote_dir/sync-admin-password.py'"
import json, os
print(json.dumps({"password": os.environ.get("SYNC_ADMIN_PASSWORD", ""), "run_id": os.environ["GITHUB_RUN_ID"]}))
PASSWORD
  exit 0
fi
if [[ "$mode" == bootstrap || "$mode" == bootstrap-rollback ]]; then
  ssh "${opts[@]}" -p "$DEPLOY_SSH_PORT" "$remote" "${prefix}python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'" || { echo 'Server Python 3.10+ is required; no production changes made' >&2; exit 1; }
  export TARGET_SHA IMAGE_REF GITHUB_ACTOR GHCR_TOKEN
  export DEPLOYMENT_ID="${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT:-1}"
  export BOOTSTRAP_ACTION="$mode"
  python3 - <<'PAYLOAD' | ssh "${opts[@]}" -p "$DEPLOY_SSH_PORT" "$remote" "${prefix}python3 -B '$remote_dir/bootstrap-production.py'"
import json, os
print(json.dumps({
    "action": os.environ["BOOTSTRAP_ACTION"], "sha": os.environ["TARGET_SHA"],
    "proxy_mode": os.environ.get("BOOTSTRAP_PROXY_MODE", "local"),
    "http_port": int(os.environ.get("BOOTSTRAP_HTTP_PORT", "3080")),
    "trusted_proxy_ips": os.environ.get("BOOTSTRAP_TRUSTED_PROXY_IPS", ""),
    "image": os.environ["IMAGE_REF"], "postgres_image": os.environ.get("POSTGRES_IMAGE", ""),
    "actor": os.environ["GITHUB_ACTOR"], "token": os.environ["GHCR_TOKEN"],
    "run_id": os.environ["DEPLOYMENT_ID"],
    "openai_api_key": os.environ.get("BOOTSTRAP_OPENAI_API_KEY", ""),
    "admin_password": os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", ""),
    "admin_password_hash": os.environ.get("BOOTSTRAP_ADMIN_PASSWORD_HASH", ""),
}))
PAYLOAD
  exit 0
fi
printf -v arguments '%q ' "$mode" "$DEPLOY_ROOT" "$TARGET_SHA" "$IMAGE_REF" "${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT:-1}" "$PUBLIC_URL" "$GITHUB_ACTOR"
# Never place registry credentials in argv, env files, SSH command text or logs.
printf '%s\n' "$GHCR_TOKEN" | ssh "${opts[@]}" -p "$DEPLOY_SSH_PORT" "$remote" "${prefix}bash '$remote_dir/remote-release.sh' $arguments"
