#!/usr/bin/env bash
set +x
set -Eeuo pipefail
umask 077
# Arguments are public deployment metadata. Registry password arrives only on stdin.
mode=${1:?deploy or rollback}; DEPLOY_ROOT=${2:?}; target_sha=${3:?}; target_image=${4:?}
deployment_id=${5:?}; public_url=${6:?}; registry_user=${7:?}
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ "$mode" == deploy || "$mode" == rollback ]] || exit 2
[[ "$DEPLOY_ROOT" =~ ^/([a-zA-Z0-9_-]+/)+[a-zA-Z0-9_-]+$ ]] || { echo 'Unsafe deployment directory' >&2; exit 2; }
[[ "$target_sha" =~ ^[0-9a-f]{40}$ && "$deployment_id" =~ ^[0-9]+-[0-9]+$ ]] || exit 2
[[ "$target_image" =~ ^ghcr\.io/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$ ]] || { echo 'Only digest-pinned GHCR images are allowed' >&2; exit 2; }
[[ "$registry_user" =~ ^[a-zA-Z0-9_-]+$ ]] || exit 2
[[ "$public_url" =~ ^https://[^[:space:]\?#]+/?$ ]] || exit 2
for tool in docker curl python3 flock timeout; do command -v "$tool" >/dev/null || { echo "Missing server tool: $tool" >&2; exit 2; }; done
[[ -f "$DEPLOY_ROOT/.env.production" && -f "$DEPLOY_ROOT/deploy.env" ]] || { echo 'Provision .env.production and deploy.env on the server first' >&2; exit 2; }
if [[ "${BOOTSTRAP_LOCAL_ONLY:-0}" != 1 ]]; then
  exec 8>"${GUIDE_GLOBAL_LOCK:-/run/lock/involta-guide-production.lock}"
  flock --wait 300 8 || { echo 'Another production operation holds the global server lock' >&2; exit 1; }
fi
exec 9>"$DEPLOY_ROOT/deploy.lock"
flock --wait 300 9 || { echo 'Another server deployment holds the lock' >&2; exit 1; }
# Parse only non-secret deployment options, never source an env file as shell code.
AUDIO_VOLUME=''; GUIDE_HTTP_PORT=3000; BASE_PATH=/guide; NGINX_NETWORK=involta-guide-frontend; NETWORK_MODE=bridge
while IFS='=' read -r key value || [[ -n "$key" ]]; do
  [[ -z "$key" || "$key" == \#* ]] && continue
  case "$key" in
    AUDIO_VOLUME) AUDIO_VOLUME=$value ;;
    GUIDE_HTTP_PORT) GUIDE_HTTP_PORT=$value ;;
    BASE_PATH) BASE_PATH=$value ;;
    NGINX_NETWORK) NGINX_NETWORK=$value ;;
    NETWORK_MODE) NETWORK_MODE=$value ;;
    POSTGRES_IMAGE) POSTGRES_IMAGE=$value ;;
    *) echo "Unknown deploy.env field: $key" >&2; exit 2 ;;
  esac
done < "$DEPLOY_ROOT/deploy.env"
[[ "$AUDIO_VOLUME" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]+$ && "$GUIDE_HTTP_PORT" =~ ^[0-9]{1,5}$ && "$BASE_PATH" =~ ^/[a-zA-Z0-9_/-]+$ && "$BASE_PATH" != */ ]] || exit 2
(( GUIDE_HTTP_PORT > 0 && GUIDE_HTTP_PORT < 65536 )) || exit 2
[[ "$NGINX_NETWORK" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]+$ ]] || exit 2
[[ "$NETWORK_MODE" == host || "$NETWORK_MODE" == bridge ]] || exit 2
export DEPLOY_ROOT AUDIO_VOLUME GUIDE_HTTP_PORT BASE_PATH NGINX_NETWORK NETWORK_MODE
# Registry credentials live in a private temporary config and are removed on exit.
unset DOCKER_HOST DOCKER_CONTEXT DOCKER_TLS DOCKER_TLS_VERIFY DOCKER_CERT_PATH
DOCKER_CONFIG=$(mktemp -d "$DEPLOY_ROOT/.registry.XXXXXXXX"); export DOCKER_CONFIG
cleanup() { rm -rf -- "$DOCKER_CONFIG"; }
trap cleanup EXIT
IFS= read -r registry_password || [[ -n "${registry_password:-}" ]]
[[ -n "${registry_password:-}" ]] || { echo 'Registry credential missing' >&2; exit 2; }
printf '%s' "$registry_password" | docker login ghcr.io --username "$registry_user" --password-stdin >/dev/null
unset registry_password
mkdir -p "$DEPLOY_ROOT/releases"
state_file="$DEPLOY_ROOT/current-release"
previous_file="$DEPLOY_ROOT/previous-release"
previous_sha=''; previous_image=''; previous_id=''
read_state() {
  local file=$1 extra=''
  read -r previous_sha previous_image previous_id extra < "$file"
  [[ -z "$extra" && "$previous_sha" =~ ^[0-9a-f]{40}$ && "$previous_image" =~ ^ghcr\.io/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$ && "$previous_id" =~ ^[0-9]+-[0-9]+$ ]] || return 1
  [[ -f "$DEPLOY_ROOT/releases/$previous_sha/compose.yaml" ]]
}
compose() {
  local sha=$1 image=$2; shift 2
  RELEASE_SHA="$sha" IMAGE_REF="$image" docker compose --project-name involta-guide-production \
    --env-file "$DEPLOY_ROOT/.env.production" --file "$DEPLOY_ROOT/releases/$sha/compose.yaml" "$@"
}
wait_health() {
  local sha=$1 image=$2 service=$3 id state
  id=$(compose "$sha" "$image" ps --quiet "$service")
  [[ -n "$id" ]] || return 1
  for _ in $(seq 1 60); do
    state=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$id")
    [[ "$state" == healthy ]] && return 0
    [[ "$state" == unhealthy || "$state" == missing ]] && return 1
    sleep 2
  done
  return 1
}
check_local() {
  local host=127.0.0.1 port=$GUIDE_HTTP_PORT
  if [[ "$NETWORK_MODE" == bridge ]]; then
    host=$(docker inspect --format "{{with index .NetworkSettings.Networks \"$NGINX_NETWORK\"}}{{.IPAddress}}{{end}}" involta-guide)
    [[ "$host" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
    port=3000
  fi
  bash "$script_dir/check-release.sh" "http://$host:$port$BASE_PATH/" "$1"
}
write_state() {
  local file=$1 sha=$2 image=$3 id=$4
  printf '%s %s %s\n' "$sha" "$image" "$id" > "$file.tmp"
  mv -f -- "$file.tmp" "$file"
}
restore_previous() {
  [[ -n "$previous_sha" ]] || return 1
  echo "Restoring previous release $previous_sha"
  timeout 300 docker pull "$previous_image" >/dev/null || return 1
  compose "$previous_sha" "$previous_image" up -d --no-build web || return 1
  wait_health "$previous_sha" "$previous_image" web || return 1
  check_local "$previous_sha" || return 1
  compose "$previous_sha" "$previous_image" up -d --no-build worker || return 1
  wait_health "$previous_sha" "$previous_image" worker || return 1
  write_state "$state_file" "$previous_sha" "$previous_image" "$previous_id"
}
# Never let Compose adopt a foreign container even if its name collides with ours.
for container in involta-guide involta-guide-production-worker-1; do
  if docker inspect "$container" >/dev/null 2>&1; then
    owner=$(docker inspect --format '{{index .Config.Labels "com.involta.guide.owner"}}' "$container")
    [[ "$owner" == production ]] || { echo "Foreign container name collision: $container; no containers changed" >&2; exit 1; }
  fi
done
if [[ "$mode" == rollback ]]; then
  [[ -f "$state_file" && -f "$previous_file" ]] || { echo 'No previous release recorded' >&2; exit 1; }
  read -r current_sha current_image current_id < "$state_file"
  [[ "$current_sha" == "$target_sha" && "$current_image" == "$target_image" && "$current_id" == "$deployment_id" ]] || { echo 'Release changed; refusing to roll back another deployment' >&2; exit 1; }
  read_state "$previous_file" || { echo 'Invalid previous release state' >&2; exit 1; }
  compose "$target_sha" "$target_image" stop --timeout 240 worker
  restore_previous
  bash "$script_dir/check-release.sh" "$public_url" "$previous_sha"
  echo 'External-check rollback completed'
  exit 0
fi
if [[ -f "$state_file" ]]; then
  read_state "$state_file" || { echo 'Invalid current release state' >&2; exit 1; }
else
  if docker inspect involta-guide >/dev/null 2>&1; then
    echo 'Existing container has no rollback state. Adopt/provision the first release explicitly.' >&2; exit 1
  fi
fi
release_dir="$DEPLOY_ROOT/releases/$target_sha"
mkdir -p "$release_dir"
# Keep each release's Compose snapshot for an exact image rollback.
template=compose.production.yaml
[[ "$NETWORK_MODE" != host ]] || template=compose.host.yaml
if [[ ! -f "$release_dir/compose.yaml" ]]; then install -m 600 "$script_dir/$template" "$release_dir/compose.yaml"; fi
timeout 300 docker pull "$target_image" >/dev/null
revision=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$target_image")
[[ "$revision" == "$target_sha" ]] || { echo 'Image revision does not match requested SHA' >&2; exit 1; }
docker volume inspect "$AUDIO_VOLUME" >/dev/null
# No migrations, seed, nginx edits, volume deletion or registry pruning here.
changed=0
failed() {
  local status=$1
  trap - ERR HUP INT TERM
  set +e
  echo "Deployment $target_sha failed; starting rollback" >&2
  compose "$target_sha" "$target_image" logs --no-color --tail 100 web worker > "$release_dir/failed.log" 2>&1
  if [[ "$changed" == 1 ]]; then
    if [[ -n "$previous_sha" ]]; then
      compose "$target_sha" "$target_image" stop --timeout 240 worker
      restore_previous && echo "Rollback to $previous_sha succeeded" >&2 || echo 'ROLLBACK FAILED: operator intervention required' >&2
    else
      # First installation: remove only our failed web/worker containers; keep all volumes and networks.
      compose "$target_sha" "$target_image" stop --timeout 240 web worker
      compose "$target_sha" "$target_image" rm --force web worker
      echo 'No prior release; failed first installation stopped. Persistent data retained.' >&2
    fi
  fi
  exit "$status"
}
trap 'failed $?' ERR
trap 'failed 143' HUP TERM
trap 'failed 130' INT
changed=1
if [[ -n "$previous_sha" ]]; then compose "$previous_sha" "$previous_image" stop --timeout 240 worker; fi
compose "$target_sha" "$target_image" up -d --no-build web
wait_health "$target_sha" "$target_image" web
check_local "$target_sha"
compose "$target_sha" "$target_image" up -d --no-build worker
wait_health "$target_sha" "$target_image" worker
if [[ "${BOOTSTRAP_LOCAL_ONLY:-0}" != 1 ]]; then bash "$script_dir/check-release.sh" "$public_url" "$target_sha"; fi
if [[ -n "$previous_sha" ]]; then write_state "$previous_file" "$previous_sha" "$previous_image" "$previous_id"; fi
write_state "$state_file" "$target_sha" "$target_image" "$deployment_id"
trap - ERR HUP INT TERM
printf '%s %s %s\n' "$(date -u +%FT%TZ)" "$target_sha" "$target_image" >> "$DEPLOY_ROOT/deployments.log" || echo 'Release committed; history log append failed' >&2
echo "Deployment $target_sha succeeded"
