#!/usr/bin/env bash
set +x
set -euo pipefail
umask 077
mode=${1:-deploy}
: "${DEPLOY_SSH_HOST:?Set repository secret DEPLOY_HOST}"
: "${DEPLOY_SSH_USER:?Set repository secret DEPLOY_USER}"
: "${DEPLOY_SSH_KEY:?Set repository secret DEPLOY_SSH_KEY}"
DEPLOY_SSH_KNOWN_HOSTS=${DEPLOY_SSH_KNOWN_HOSTS:-}
: "${TARGET_SHA:?}" "${IMAGE_REF:?}" "${GHCR_TOKEN:?}" "${GITHUB_ACTOR:?}" "${GITHUB_RUN_ID:?}"
DEPLOY_SSH_PORT=${DEPLOY_SSH_PORT:-22}; DEPLOY_ROOT=${DEPLOY_ROOT:-/opt/involta-guide}
PUBLIC_URL=${PUBLIC_URL:-https://involta.ru/guide/}
[[ "$mode" == deploy || "$mode" == rollback || "$mode" == bootstrap || "$mode" == bootstrap-rollback ]] || exit 2
[[ "$DEPLOY_SSH_HOST" =~ ^[a-zA-Z0-9.-]+$ && "$DEPLOY_SSH_USER" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ && "$DEPLOY_SSH_PORT" =~ ^[0-9]{1,5}$ ]] || exit 2
(( DEPLOY_SSH_PORT > 0 && DEPLOY_SSH_PORT < 65536 )) || exit 2
[[ "$DEPLOY_ROOT" =~ ^/([a-zA-Z0-9_-]+/)+[a-zA-Z0-9_-]+$ && "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]] || exit 2
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
cleanup() {
  ssh "${opts[@]}" -p "$DEPLOY_SSH_PORT" "$remote" "rm -rf -- '$remote_dir'" </dev/null >/dev/null 2>&1 || true
  rm -rf -- "$work"
}
trap cleanup EXIT
ssh "${opts[@]}" -p "$DEPLOY_SSH_PORT" "$remote" "umask 077; mkdir '$remote_dir'"
scp "${opts[@]}" -P "$DEPLOY_SSH_PORT" deploy/remote-release.sh deploy/check-release.sh deploy/compose.production.yaml deploy/compose.database.yaml deploy/compose.host.yaml deploy/compose.database-host.yaml deploy/bootstrap-production.py deploy/nginx_config.py "$remote:$remote_dir/"
# Check privilege before executing any production tooling. No password prompts.
privilege=$(ssh "${opts[@]}" -p "$DEPLOY_SSH_PORT" "$remote" 'if [ "$(id -u)" = 0 ]; then echo root; elif command -v sudo >/dev/null && sudo -n true; then echo sudo; else echo "Root or passwordless sudo is required" >&2; exit 1; fi')
prefix=''
[[ "$privilege" == root ]] || { [[ "$privilege" == sudo ]] || exit 1; prefix='sudo -n '; }
if [[ "$mode" == bootstrap || "$mode" == bootstrap-rollback ]]; then
  ssh "${opts[@]}" -p "$DEPLOY_SSH_PORT" "$remote" "${prefix}python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'" || { echo 'Server Python 3.10+ is required; no production changes made' >&2; exit 1; }
  export TARGET_SHA IMAGE_REF GITHUB_ACTOR GHCR_TOKEN
  export DEPLOYMENT_ID="${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT:-1}"
  export BOOTSTRAP_ACTION="$mode"
  python3 - <<'PAYLOAD' | ssh "${opts[@]}" -p "$DEPLOY_SSH_PORT" "$remote" "${prefix}python3 -B '$remote_dir/bootstrap-production.py'"
import json, os
print(json.dumps({
    "action": os.environ["BOOTSTRAP_ACTION"], "sha": os.environ["TARGET_SHA"],
    "image": os.environ["IMAGE_REF"], "postgres_image": os.environ.get("POSTGRES_IMAGE", ""),
    "actor": os.environ["GITHUB_ACTOR"], "token": os.environ["GHCR_TOKEN"],
    "run_id": os.environ["DEPLOYMENT_ID"],
    "openai_api_key": os.environ.get("BOOTSTRAP_OPENAI_API_KEY", ""),
    "admin_password": os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", ""),
    "admin_password_hash": os.environ.get("BOOTSTRAP_ADMIN_PASSWORD_HASH", ""),
    "alex_secret": os.environ.get("BOOTSTRAP_ALEX_SECRET", ""),
}))
PAYLOAD
  exit 0
fi
printf -v arguments '%q ' "$mode" "$DEPLOY_ROOT" "$TARGET_SHA" "$IMAGE_REF" "${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT:-1}" "$PUBLIC_URL" "$GITHUB_ACTOR"
# Never place registry credentials in argv, env files, SSH command text or logs.
printf '%s\n' "$GHCR_TOKEN" | ssh "${opts[@]}" -p "$DEPLOY_SSH_PORT" "$remote" "${prefix}bash '$remote_dir/remote-release.sh' $arguments"
