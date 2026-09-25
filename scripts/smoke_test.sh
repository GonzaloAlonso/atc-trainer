#!/usr/bin/env bash
# Start a built image and check it behaves like a release candidate.
#   scripts/smoke_test.sh <image> [expected-version]
set -euo pipefail

IMAGE="$1"
EXPECTED_VERSION="${2:-}"
NAME="visor-smoke-$$"
PORT="${SMOKE_PORT:-18000}"

cleanup() {
  status=$?
  if [ $status -ne 0 ]; then
    echo "---- container logs ----"
    docker logs "$NAME" 2>&1 | tail -50 || true
  fi
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  exit $status
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

# VISOR_RECORD=0: never call OpenSky from CI.
docker run -d --name "$NAME" -p "127.0.0.1:${PORT}:8000" -e VISOR_RECORD=0 "$IMAGE" >/dev/null

echo "waiting for the container healthcheck..."
for _ in $(seq 1 60); do
  health=$(docker inspect -f '{{.State.Health.Status}}' "$NAME")
  [ "$health" = "healthy" ] && break
  [ "$health" = "unhealthy" ] && fail "container reported unhealthy"
  sleep 2
done
[ "$health" = "healthy" ] || fail "container not healthy after 120 s (status: $health)"

BASE="http://127.0.0.1:${PORT}"
status=$(curl -fsS "$BASE/api/status")
version=$(echo "$status" | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])')
echo "reported version: $version"
if [ -n "$EXPECTED_VERSION" ] && [ "$version" != "$EXPECTED_VERSION" ]; then
  fail "version mismatch: expected $EXPECTED_VERSION, got $version"
fi

label=$(docker inspect -f '{{ index .Config.Labels "org.opencontainers.image.version" }}' "$IMAGE")
[ -z "$EXPECTED_VERSION" ] || [ "$label" = "$EXPECTED_VERSION" ] || fail "image label version is '$label'"

curl -fsS "$BASE/" | grep -q "Visor ATC" || fail "index page"
curl -fsS "$BASE/js/main.js" >/dev/null || fail "static assets"
curl -fsS "$BASE/docs" >/dev/null || fail "OpenAPI docs"

fixes=$(curl -fsS "$BASE/api/navdata" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["fixes"]))')
echo "navdata fixes: $fixes"
[ "$fixes" -gt 1000 ] || fail "navdata missing from image"

curl -fsS "$BASE/api/observation" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert {"aircraft","conflicts","decisions","score"} <= d.keys()' \
  || fail "observation schema"

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/command" -H 'Content-Type: application/json' -d '{"text":"NOBODY C 350"}')
[ "$code" = "400" ] || fail "command validation returned $code"

user=$(docker exec "$NAME" id -u)
[ "$user" != "0" ] || fail "container runs as root"

echo "smoke test passed"
