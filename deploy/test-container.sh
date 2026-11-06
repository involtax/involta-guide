#!/usr/bin/env bash
# Run checks in the cached environment, against this checkout (never cached source).
set -euo pipefail
root=$(git rev-parse --show-toplevel)
cd "$root"
mkdir -p "$root/var/ci-tmp"
compose=$(docker info --format '{{range .ClientInfo.Plugins}}{{if eq .Name "compose"}}{{.Path}}{{end}}{{end}}')
[[ -f "$compose" ]] || { echo 'Docker Compose plugin is required for deployment tests.' >&2; exit 1; }
args=(
  --rm --init --network host --shm-size 1g
  --user "$(id -u):$(id -g)" --group-add "$(stat -c %g /var/run/docker.sock)"
  --mount "type=bind,src=$root,dst=$root"
  --mount "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock"
  --mount "type=bind,src=$(command -v docker),dst=/usr/local/bin/docker,readonly"
  --mount "type=bind,src=$compose,dst=/usr/local/lib/docker/cli-plugins/docker-compose,readonly"
  --workdir "$root"
  --env "PYTHONPATH=$root/src" --env "TMPDIR=$root/var/ci-tmp" --env HOME=/tmp
)
# Worktrees have Git metadata outside the checkout; CI's normal clone needs none.
git_common=$(git rev-parse --path-format=absolute --git-common-dir)
if [[ "$git_common" != "$root/"* ]]; then
  args+=(--mount "type=bind,src=$git_common,dst=$git_common,readonly")
fi
# Explicit allowlist: no API keys, GitHub tokens or production secrets enter tests.
for key in TEST_DATABASE_URL DATABASE_URL SESSION_SECRET VOICE_TEST_IMAGE GUIDE_TEST_IMAGE \
  GUIDE_TEST_NETWORK GUIDE_CONTAINER_DATABASE_URL; do
  if [[ -v "$key" ]]; then args+=(--env "$key"); fi
done
exec docker run "${args[@]}" involta-guide:test "$@"
