#!/usr/bin/env bash
set +x
set -euo pipefail
# A public check independent of SSH/container state; secrets are never sent.
base_url=${1:?Public guide URL required}
expected_sha=${2:?Commit SHA required}
proxy_mode=${3:-local}
[[ "$proxy_mode" == local || "$proxy_mode" == external ]] || exit 2
[[ "$base_url" =~ ^https?://[^[:space:]\?#]+/?$ ]] || { echo 'Invalid public URL' >&2; exit 2; }
[[ "$expected_sha" =~ ^[0-9a-f]{40}$ ]] || { echo 'Expected a full commit SHA' >&2; exit 2; }
base_url=${base_url%/}
body=$(curl --retry 2 --retry-delay 2 --retry-all-errors --fail --silent --show-error --connect-timeout 10 --max-time 20 \
  --header 'Cache-Control: no-cache' --url "$base_url/health/ready?revision=$expected_sha")
printf '%s' "$body" | python3 -c 'import json,sys; data=json.load(sys.stdin); assert data.get("status")=="ready" and data.get("revision")==sys.argv[1], "Unexpected running release"' "$expected_sha"
for path in / /widget.js /assets/style.css /assets/htmx.min.js /admin \
  /voice/widget.v1.js /voice/vad/ort.wasm.min.js /voice/vad/bundle.min.js \
  /voice/vad/vad.worklet.bundle.min.js /voice/vad/ort-wasm-simd-threaded.mjs \
  /voice/vad/ort-wasm-simd-threaded.wasm /voice/vad/silero_vad_v5.onnx; do
  metadata=$(curl --retry 2 --retry-delay 2 --retry-all-errors --fail --silent --show-error --connect-timeout 10 --max-time 20 \
    --output /dev/null --write-out '%{http_code} %{content_type}' --url "$base_url$path")
  case "$path" in
    *.css) [[ "$metadata" == '200 text/css'* ]] ;;
    *.js|*.mjs) [[ "$metadata" == '200 application/javascript'* || "$metadata" == '200 text/javascript'* ]] ;;
    *.wasm) [[ "$metadata" == '200 application/wasm'* ]] ;;
    *.onnx) [[ "$metadata" == '200 application/octet-stream'* ]] ;;
    *) [[ "$metadata" == '200 text/html'* ]] ;;
  esac || { echo "Unexpected status/content type at $path: $metadata" >&2; exit 1; }
done
redirect=$(curl --retry 2 --retry-delay 2 --retry-all-errors --silent --show-error --connect-timeout 10 --max-time 20 \
  --output /dev/null --write-out '%{http_code} %{redirect_url}' --url "$base_url?probe=guide-check")
if [[ "$proxy_mode" == local ]]; then
  [[ "$redirect" == "302 $base_url/?probe=guide-check" ]] || { echo "Expected backend /guide → 302 /guide/ with query preserved; got: $redirect" >&2; exit 1; }
else
  accepted=0
  for status in 301 302 307 308; do
    if [[ "$redirect" == "$status $base_url/" || "$redirect" == "$status $base_url/?probe=guide-check" ]]; then accepted=1; break; fi
  done
  [[ "$accepted" == 1 ]] || { echo "Unexpected external proxy redirect: $redirect" >&2; exit 1; }
  printf 'External nginx redirect: %s (backend 302 contract checked locally).\n' "$redirect"
fi
printf 'Release %s is ready and serves public/admin/widget assets.\n' "$expected_sha"
