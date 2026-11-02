#!/usr/bin/env bash
set +x
set -euo pipefail
# A public check independent of SSH/container state; secrets are never sent.
base_url=${1:?Public guide URL required}
expected_sha=${2:?Commit SHA required}
[[ "$base_url" =~ ^https?://[^[:space:]\?#]+/?$ ]] || { echo 'Invalid public URL' >&2; exit 2; }
[[ "$expected_sha" =~ ^[0-9a-f]{40}$ ]] || { echo 'Expected a full commit SHA' >&2; exit 2; }
base_url=${base_url%/}
body=$(curl --fail --silent --show-error --connect-timeout 5 --max-time 15 \
  --header 'Cache-Control: no-cache' --url "$base_url/health/ready")
printf '%s' "$body" | python3 -c 'import json,sys; data=json.load(sys.stdin); assert data.get("status")=="ready" and data.get("revision")==sys.argv[1], "Unexpected running release"' "$expected_sha"
for path in / /widget.js /assets/style.css /assets/htmx.min.js /admin; do
  metadata=$(curl --fail --silent --show-error --connect-timeout 5 --max-time 15 \
    --output /dev/null --write-out '%{http_code} %{content_type}' --url "$base_url$path")
  case "$path" in
    *.css) [[ "$metadata" == '200 text/css'* ]] ;;
    *.js) [[ "$metadata" == '200 application/javascript'* || "$metadata" == '200 text/javascript'* ]] ;;
    *) [[ "$metadata" == '200 text/html'* ]] ;;
  esac || { echo "Unexpected status/content type at $path" >&2; exit 1; }
done
redirect=$(curl --silent --show-error --connect-timeout 5 --max-time 15 \
  --output /dev/null --write-out '%{http_code} %{redirect_url}' --url "$base_url?probe=guide-check")
[[ "$redirect" == "302 $base_url/?probe=guide-check" ]] || { echo 'Expected /guide → 302 /guide/ with query preserved' >&2; exit 1; }
printf 'Release %s is ready and serves public/admin/widget assets.\n' "$expected_sha"
