#!/usr/bin/env bash
# Start a built image and check it behaves like a release candidate.
#   scripts/smoke_test.sh <image> [expected-version] [expected-commit]
set -euo pipefail

IMAGE="$1"
EXPECTED_VERSION="${2:-}"
EXPECTED_COMMIT="${3:-}"
NAME="visor-smoke-$$"
PORT="${SMOKE_PORT:-18000}"

cleanup() {
  status=$?
  if [ $status -ne 0 ]; then
    echo "---- container logs ----"
    docker logs "$NAME" 2>&1 | tail -50 || true
  fi
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  rm -f "$JAR"
  exit $status
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

ADMIN_PASSWORD="smoke-test-$$-password"
JAR=$(mktemp)
# VISOR_RECORD=0: never call OpenSky from CI.
docker run -d --name "$NAME" -p "127.0.0.1:${PORT}:8000" -e VISOR_RECORD=0 \
  -e VISOR_ADMIN_USER=admin -e VISOR_ADMIN_PASSWORD="$ADMIN_PASSWORD" "$IMAGE" >/dev/null

echo "waiting for the container healthcheck..."
for _ in $(seq 1 60); do
  health=$(docker inspect -f '{{.State.Health.Status}}' "$NAME")
  [ "$health" = "healthy" ] && break
  [ "$health" = "unhealthy" ] && fail "container reported unhealthy"
  sleep 2
done
[ "$health" = "healthy" ] || fail "container not healthy after 120 s (status: $health)"

BASE="http://127.0.0.1:${PORT}"
# curl with the session cookie
acurl() { curl -fsS -b "$JAR" -c "$JAR" "$@"; }

# anonymous access is refused
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/status")
[ "$code" = "401" ] || fail "unauthenticated /api/status returned $code"
loc=$(curl -s -o /dev/null -w '%{redirect_url}' "$BASE/")
case "$loc" in */login*) ;; *) fail "/ does not redirect to the login page ($loc)" ;; esac
curl -fsS "$BASE/login" | grep -q "Sign in" || fail "login page"
curl -fsS "$BASE/api/health" >/dev/null || fail "health endpoint"

# the bootstrap admin can sign in
acurl -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"$ADMIN_PASSWORD\"}" >/dev/null || fail "admin login"
acurl "$BASE/api/admin/users" | grep -q '"username":"admin"' || fail "admin user list"

status=$(acurl "$BASE/api/status")
version=$(echo "$status" | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])')
echo "reported version: $version"
if [ -n "$EXPECTED_VERSION" ] && [ "$version" != "$EXPECTED_VERSION" ]; then
  fail "version mismatch: expected $EXPECTED_VERSION, got $version"
fi

label=$(docker inspect -f '{{ index .Config.Labels "org.opencontainers.image.version" }}' "$IMAGE")
[ -z "$EXPECTED_VERSION" ] || [ "$label" = "$EXPECTED_VERSION" ] || fail "image label version is '$label'"

about=$(acurl "$BASE/api/about")
echo "$about" | python3 -c '
import json, sys
a = json.load(sys.stdin)
exp_version, exp_commit = sys.argv[1], sys.argv[2]
assert "Gonzalo Alonso" in a["copyright"] and "Gonzalo Alonso" in a["credits"], "copyright"
assert not exp_version or a["version"] == exp_version, "about version"
assert not exp_commit or a["build"]["commit"] == exp_commit, "about commit"
print("about:", a["version"], a["build"]["commit_short"], a["build"]["date"], "|", a["copyright"])
' "$EXPECTED_VERSION" "$EXPECTED_COMMIT" || fail "about information"

acurl "$BASE/" | grep -q "Visor ATC" || fail "index page"
acurl "$BASE/js/main.js" >/dev/null || fail "static assets"
acurl "$BASE/admin" | grep -q "Add user" || fail "admin page"
acurl "$BASE/docs" >/dev/null || fail "OpenAPI docs"

fixes=$(acurl "$BASE/api/navdata" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["fixes"]))')
echo "navdata fixes: $fixes"
[ "$fixes" -gt 1000 ] || fail "navdata missing from image"

acurl "$BASE/api/observation" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert {"aircraft","conflicts","decisions","score"} <= d.keys()' \
  || fail "observation schema"

code=$(curl -s -o /dev/null -w '%{http_code}' -b "$JAR" -X POST "$BASE/api/command" -H 'Content-Type: application/json' -d '{"text":"NOBODY C 350"}')
[ "$code" = "400" ] || fail "command validation returned $code"

user=$(docker exec "$NAME" id -u)
[ "$user" != "0" ] || fail "container runs as root"

echo "smoke test passed"
