#!/usr/bin/env bash
# Prove the locked network is default-deny + allowlist — WITHOUT starting the real
# agent (zero production impact). Brings up ONLY the egress-proxy and the two
# networks, then runs a throwaway client container on the internal net and asserts:
#
#   1. allowlisted host via proxy        -> REACHABLE      (api.tavily.com:443)
#   2. non-allowlisted host via proxy    -> BLOCKED (L7)   (example.com:443)
#   3. external IP DIRECT (no proxy)     -> NO ROUTE (L3)  (1.1.1.1)  <- the real proof
#   4. allowlisted host DIRECT (no proxy)-> NO ROUTE (L3)  (proves even allowed
#                                                            hosts need the proxy)
#   5. host model proxy via proxy        -> REACHABLE      (host.docker.internal:8787)
#
# Tests 3+4 are the ones that matter most: they prove enforcement is at the network
# layer (no route), not merely proxy policy an injected client could ignore.
#
# Usage:  ./validate_egress.sh        (from this directory)
set -euo pipefail
cd "$(dirname "$0")"

PROJECT=hermes-egresscheck
COMPOSE=(docker compose -p "$PROJECT" -f docker-compose.locked.yml)
NET="${PROJECT}_hermes-internal"
CLIENT_IMG="curlimages/curl:8.10.1"
PROXY="http://egress-proxy:8888"
PASS=0 FAIL=0

cleanup() { echo "--- tearing down ---"; "${COMPOSE[@]}" down -v >/dev/null 2>&1 || true; }
trap cleanup EXIT

# run a curl in a throwaway container on the internal net. echoes nothing; returns curl's exit code.
client() { docker run --rm --network "$NET" "$CLIENT_IMG" "$@"; }

# assert_ok  <label> <curl-args...>   -> expects curl to SUCCEED (exit 0)
assert_ok() {
  local label="$1"; shift
  if client -sS -o /dev/null --max-time 20 "$@" >/dev/null 2>&1; then
    echo "  PASS  $label (reachable, as expected)"; PASS=$((PASS+1))
  else
    echo "  FAIL  $label (expected REACHABLE but it was blocked)"; FAIL=$((FAIL+1))
  fi
}

# assert_blocked <label> <curl-args...> -> expects curl to FAIL (non-zero)
assert_blocked() {
  local label="$1"; shift
  if client -sS -o /dev/null --max-time 12 "$@" >/dev/null 2>&1; then
    echo "  FAIL  $label (expected BLOCKED but it got through!)"; FAIL=$((FAIL+1))
  else
    echo "  PASS  $label (blocked, as expected)"; PASS=$((PASS+1))
  fi
}

echo "=== build + start egress-proxy only (agent NOT started) ==="
"${COMPOSE[@]}" up -d --build egress-proxy
echo "--- waiting for proxy to answer ---"
for i in $(seq 1 20); do
  if client -sS -o /dev/null --max-time 5 -x "$PROXY" http://host.docker.internal:8787/healthz >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

echo "=== egress assertions ==="
# 1. allowlisted via proxy — a 401/400 from Tavily still means the tunnel opened.
assert_ok      "[1] api.tavily.com via proxy"        -x "$PROXY" https://api.tavily.com
# 2. non-allowlisted via proxy — tinyproxy FilterDefaultDeny returns 403.
assert_blocked "[2] example.com via proxy"           -x "$PROXY" https://example.com
# 3. external IP, DIRECT (bypass proxy) — internal net has no route. THE core proof.
assert_blocked "[3] 1.1.1.1 DIRECT (no proxy)"       --noproxy '*' https://1.1.1.1
# 4. allowlisted host, DIRECT — also no route (only the proxy can reach it).
assert_blocked "[4] api.tavily.com DIRECT (no proxy)" --noproxy '*' https://api.tavily.com
# 5. host model proxy via proxy — proves the model path survives the lock.
assert_ok      "[5] host model proxy via proxy"      -x "$PROXY" http://host.docker.internal:8787/healthz

echo "=== result: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
