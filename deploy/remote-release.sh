#!/usr/bin/env bash
set +x
set -Eeuo pipefail
umask 077
# Arguments are public deployment metadata. Registry password arrives only on stdin.
mode=${1:?deploy or rollback}; DEPLOY_ROOT=${2:?}; target_sha=${3:?}; target_image=${4:?}
deployment_id=${5:?}; public_url=${6:?}; registry_user=${7:?}
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ "$mode" == deploy || "$mode" == rollback ]] || exit 2
[[ "$mode" != rollback ]] || export VOICE_INTAKE=false
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
VOICE_STACK=0
AUDIO_VOLUME=''; GUIDE_HTTP_PORT=3000; BASE_PATH=/guide; NGINX_NETWORK=involta-guide-frontend; NETWORK_MODE=bridge; GUIDE_BIND_ADDRESS=127.0.0.1; GUIDE_APPARMOR_PROFILE=docker-default
while IFS='=' read -r key value || [[ -n "$key" ]]; do
  [[ -z "$key" || "$key" == \#* ]] && continue
  case "$key" in
    VOICE_STACK) [[ "$value" == 0 || "$value" == 1 ]] || exit 2; VOICE_STACK=$value ;;
    AUDIO_VOLUME) AUDIO_VOLUME=$value ;;
    GUIDE_HTTP_PORT) GUIDE_HTTP_PORT=$value ;;
    GUIDE_BIND_ADDRESS) GUIDE_BIND_ADDRESS=$value ;;
    GUIDE_APPARMOR_PROFILE) GUIDE_APPARMOR_PROFILE=$value ;;
    BASE_PATH) BASE_PATH=$value ;;
    NGINX_NETWORK) NGINX_NETWORK=$value ;;
    NETWORK_MODE) NETWORK_MODE=$value ;;
    POSTGRES_IMAGE) [[ "$value" =~ ^postgres@sha256:[0-9a-f]{64}$ ]] || exit 2 ;;
    *) echo "Unknown deploy.env field: $key" >&2; exit 2 ;;
  esac
done < "$DEPLOY_ROOT/deploy.env"
[[ "$AUDIO_VOLUME" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]+$ && "$GUIDE_HTTP_PORT" =~ ^[0-9]{1,5}$ && "$BASE_PATH" =~ ^/[a-zA-Z0-9_/-]+$ && "$BASE_PATH" != */ ]] || exit 2
(( GUIDE_HTTP_PORT > 0 && GUIDE_HTTP_PORT < 65536 )) || exit 2
[[ "$NGINX_NETWORK" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]+$ ]] || exit 2
[[ "$NETWORK_MODE" == host || "$NETWORK_MODE" == bridge ]] || exit 2
[[ "$GUIDE_BIND_ADDRESS" == 127.0.0.1 || "$GUIDE_BIND_ADDRESS" == 0.0.0.0 ]] || exit 2
[[ "$GUIDE_APPARMOR_PROFILE" == docker-default || "$GUIDE_APPARMOR_PROFILE" == unconfined ]] || exit 2
export GUIDE_APPARMOR_PROFILE
export DEPLOY_ROOT AUDIO_VOLUME GUIDE_HTTP_PORT GUIDE_BIND_ADDRESS BASE_PATH NGINX_NETWORK NETWORK_MODE
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
  local extra=() voice_env=()
  if [[ -f "$DEPLOY_ROOT/releases/$sha/compose.voice.yaml" ]]; then extra=(-f "$DEPLOY_ROOT/releases/$sha/compose.voice.yaml"); voice_env=(--env-file "$DEPLOY_ROOT/.env.voice"); fi
  RELEASE_SHA="$sha" IMAGE_REF="$image" docker compose --project-name involta-guide-production \
    --env-file "$DEPLOY_ROOT/.env.production" "${voice_env[@]}" --file "$DEPLOY_ROOT/releases/$sha/compose.yaml" "${extra[@]}" "$@"
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
check_public() {
  local proxy_mode=local
  [[ "$GUIDE_BIND_ADDRESS" != 0.0.0.0 ]] || proxy_mode=external
  bash "$script_dir/check-release.sh" "$public_url" "$1" "$proxy_mode"
}
write_state() {
  local file=$1 sha=$2 image=$3 id=$4
  printf '%s %s %s\n' "$sha" "$image" "$id" > "$file.tmp"
  mv -f -- "$file.tmp" "$file"
}
stop_guide_writers() {
  local sha=$1 image=$2
  compose "$sha" "$image" stop --timeout 240 worker web
  if [[ -f "$DEPLOY_ROOT/releases/$sha/compose.voice.yaml" ]]; then
    compose "$sha" "$image" stop --timeout 240 voice-consumer
  fi
}
restore_cutover_database() {
  local backup="$DEPLOY_ROOT/releases/$target_sha/pre-cutover.$deployment_id.dump"
  [[ -f "$backup" ]] || return 0
  stop_guide_writers "$target_sha" "$target_image" || return 1
  compose "$target_sha" "$target_image" run --rm --no-deps -T web \
    python -m involta_guide.infrastructure.persistence.release_backup restore < "$backup" || return 1
}
restore_previous() {
  export VOICE_INTAKE=false
  [[ -n "$previous_sha" ]] || return 1
  restore_cutover_database || return 1
  echo "Restoring previous release $previous_sha"
  docker image inspect "$previous_image" >/dev/null 2>&1 || timeout 300 docker pull "$previous_image" >/dev/null || return 1
  compose "$previous_sha" "$previous_image" up -d --no-build web || return 1
  wait_health "$previous_sha" "$previous_image" web || return 1
  check_local "$previous_sha" || return 1
  # An older worker must not turn a pending background GET into another paid POST.
  compose "$previous_sha" "$previous_image" run --rm --no-deps -T web python - < "$script_dir/pause-background.py" || return 1
  compose "$previous_sha" "$previous_image" up -d --no-build worker || return 1
  wait_health "$previous_sha" "$previous_image" worker || return 1
  # Roll back Voice executors as well, retaining their current volumes/schema.
  # The gateway-compatible baseline may predate the overlay, so reuse the current
  # service definition with its reviewed previous image and no dependency recreation.
  if [[ -f "$DEPLOY_ROOT/releases/$target_sha/compose.voice.yaml" ]]; then
    compose "$target_sha" "$previous_image" up -d --no-build --no-deps voice-api voice-worker voice-consumer || return 1
    wait_health "$target_sha" "$previous_image" voice-api || return 1
    wait_health "$target_sha" "$previous_image" voice-worker || return 1
    wait_health "$target_sha" "$previous_image" voice-consumer || return 1
  fi
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
  check_public "$previous_sha"
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
if [[ "$VOICE_STACK" == 1 ]]; then
  [[ "$mode" != rollback ]] || export VOICE_INTAKE=false
  voice_postgres_image=''
  while IFS='=' read -r key value; do
    if [[ "$key" == VOICE_POSTGRES_IMAGE ]]; then voice_postgres_image=$value; fi
  done < "$DEPLOY_ROOT/.env.voice"
  [[ "$voice_postgres_image" =~ ^postgres@sha256:[0-9a-f]{64}$ ]] || { echo 'Voice PostgreSQL image must be digest-pinned' >&2; exit 2; }
  [[ -f "$DEPLOY_ROOT/.env.voice" ]] || { echo 'Provision .env.voice first' >&2; exit 2; }
  voice_template=compose.voice.yaml
  [[ "$NETWORK_MODE" != host ]] || voice_template=compose.voice.host.yaml
  install -m 600 "$script_dir/$voice_template" "$release_dir/compose.voice.yaml"
  # An automatic rollback to a pre-Voice image would break persisted voice: references.
  if [[ -n "$previous_sha" ]]; then
    docker run --rm --entrypoint python "$previous_image" -c 'import involta_voice; import involta_guide.web.routes.voice' || { echo 'Rollback image must support Voice before enabling the stack' >&2; exit 2; }
  fi
fi
timeout 300 docker pull "$target_image" >/dev/null
revision=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$target_image")
[[ "$revision" == "$target_sha" ]] || { echo 'Image revision does not match requested SHA' >&2; exit 1; }
if [[ "$mode" == rollback && -n "$previous_sha" && -f "$DEPLOY_ROOT/releases/$previous_sha/compose.voice.yaml" ]]; then
  docker run --rm --entrypoint python "$target_image" -c 'import involta_voice; import involta_guide.web.routes.voice' || { echo 'Cannot roll back Voice references to an incompatible image' >&2; exit 2; }
fi
docker volume inspect "$AUDIO_VOLUME" >/dev/null
# The current-state cutover has a private pre-change dump; image rollback restores it first.
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
      restore_cutover_database || { echo "DATABASE RESTORE FAILED" >&2; exit "$status"; }
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
if [[ -n "$previous_sha" ]]; then stop_guide_writers "$previous_sha" "$previous_image"; fi
needs_backup=$(compose "$target_sha" "$target_image" run --rm --no-deps -T web python -m involta_guide.infrastructure.persistence.release_backup needed)
if [[ "$needs_backup" == yes ]]; then
  compose "$target_sha" "$target_image" run --rm --no-deps -T web \
    python -m involta_guide.infrastructure.persistence.release_backup backup > "$release_dir/pre-cutover.$deployment_id.dump.tmp"
  [[ -s "$release_dir/pre-cutover.$deployment_id.dump.tmp" ]] || { echo 'Empty database backup; migration refused' >&2; false; }
  mv -f -- "$release_dir/pre-cutover.$deployment_id.dump.tmp" "$release_dir/pre-cutover.$deployment_id.dump"
elif [[ "$needs_backup" != no ]]; then
  echo 'Cannot determine schema compatibility; migration refused' >&2; false
fi
compose "$target_sha" "$target_image" run --rm --no-deps web alembic upgrade head
if [[ "$VOICE_STACK" == 1 ]]; then
  compose "$target_sha" "$target_image" up -d --no-build voice-db
  compose "$target_sha" "$target_image" run --rm voice-migrate
  compose "$target_sha" "$target_image" run --rm voice-prepare-model
  compose "$target_sha" "$target_image" up -d --no-build voice-api voice-worker voice-consumer
  wait_health "$target_sha" "$target_image" voice-api
  wait_health "$target_sha" "$target_image" voice-worker
  wait_health "$target_sha" "$target_image" voice-consumer
fi
compose "$target_sha" "$target_image" up -d --no-build web
wait_health "$target_sha" "$target_image" web
check_local "$target_sha"
compose "$target_sha" "$target_image" up -d --no-build worker
wait_health "$target_sha" "$target_image" worker
if [[ "${BOOTSTRAP_LOCAL_ONLY:-0}" != 1 ]]; then check_public "$target_sha"; fi
if [[ -n "$previous_sha" ]]; then write_state "$previous_file" "$previous_sha" "$previous_image" "$previous_id"; fi
write_state "$state_file" "$target_sha" "$target_image" "$deployment_id"
trap - ERR HUP INT TERM
printf '%s %s %s\n' "$(date -u +%FT%TZ)" "$target_sha" "$target_image" >> "$DEPLOY_ROOT/deployments.log" || echo 'Release committed; history log append failed' >&2
echo "Deployment $target_sha succeeded"
