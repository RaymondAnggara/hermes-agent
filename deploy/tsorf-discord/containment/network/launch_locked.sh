#!/usr/bin/env bash
# Phase 5b secrets-hardening: launch (or restart) the locked container stack with
# DISCORD_BOT_TOKEN + TAVILY_API_KEY injected from Bitwarden Secrets Manager.
#
# WHY this exists (containment-(c) for the platform tokens):
#   The agent runs in the locked container with ~/.hermes mounted at /opt/data, so
#   anything in /opt/data/.env is plaintext the prompt-injectable agent can `cat`.
#   The model key was already removed from .env (W3: host model-key proxy injects it
#   upstream). discord.py and the Tavily client, by contrast, need their tokens
#   IN-PROCESS — so we cannot hide them from the agent the way the model key is
#   hidden. What we CAN do, and do here, is:
#     1. keep them OUT of the at-rest mounted .env (removed from ~/.hermes/.env), and
#     2. keep the Bitwarden BOOTSTRAP token (BWS_ACCESS_TOKEN) OUT of the container.
#   This script is the host-side launcher that holds the bootstrap (via Keychain),
#   fetches only the two final token VALUES from Bitwarden, and injects them into the
#   container's process env at launch. The bootstrap never enters the container; the
#   final values are never written to disk.
#
# Threat model honesty: the agent can still read its own /proc/self/environ and see
# DISCORD_BOT_TOKEN / TAVILY_API_KEY (unavoidable — the gateway authenticates with
# them). The egress secret-redaction layer (gateway/secret_egress.py, value layer)
# scrubs their literal values from outbound Discord messages.
#
# Usage:
#   ./launch_locked.sh            # fetch tokens, (re)create the agent container
#   ./launch_locked.sh --dry-run  # resolve+confirm tokens, do NOT touch docker
#
# This REPLACES the bare `docker compose ... up -d` in CONTAINMENT.md's deploy model
# for the locked stack: config/code changes must come up through THIS script so the
# tokens are present (the compose file fails closed if they are not — see below).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KEYCHAIN_SH="$HERE/../keychain_bootstrap.sh"
COMPOSE_FILE="$HERE/docker-compose.locked.yml"
PROJECT_ID="ea7a76db-b22b-4f0c-bc85-b47100d68d2a"   # Bitwarden "Hermes Agent" project
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

command -v bws >/dev/null 2>&1 || { echo "FATAL: bws not on PATH" >&2; exit 70; }

# 1) Bootstrap: read the Bitwarden access token from the macOS Keychain (NOT plaintext
#    on disk, NEVER passed into the container). Reuses the model-key bootstrap helper.
if [ -z "${BWS_ACCESS_TOKEN:-}" ]; then
  BWS_ACCESS_TOKEN="$(KC_SERVICE=hermes-bws-access-token KC_ACCOUNT=bws bash "$KEYCHAIN_SH" read 2>/dev/null || true)"
fi
[ -n "${BWS_ACCESS_TOKEN:-}" ] || { echo "FATAL: no BWS_ACCESS_TOKEN (Keychain item hermes-bws-access-token/bws missing?)" >&2; exit 71; }
export BWS_ACCESS_TOKEN

# 2) Resolve secret NAME -> id within the project (robust to id rotation), then fetch
#    the value. We never echo a value; we confirm only its length.
fetch_secret() {  # $1 = secret key name -> prints value on stdout
  local name="$1"
  bws secret list "$PROJECT_ID" --output json 2>/dev/null \
    | BWS_NAME="$name" python3 -c '
import sys, json, os, subprocess
name = os.environ["BWS_NAME"]
items = json.load(sys.stdin)
sid = next((s["id"] for s in items if s["key"] == name), None)
if not sid:
    sys.stderr.write(f"FATAL: secret {name} not found in project\n"); sys.exit(72)
out = subprocess.run(["bws", "secret", "get", sid, "--output", "json"],
                     capture_output=True, text=True)
if out.returncode != 0:
    sys.stderr.write(f"FATAL: bws get {name} failed: {out.stderr}\n"); sys.exit(73)
val = (json.loads(out.stdout).get("value") or "")
if not val:
    sys.stderr.write(f"FATAL: secret {name} is empty\n"); sys.exit(74)
sys.stdout.write(val)
'
}

DISCORD_BOT_TOKEN="$(fetch_secret DISCORD_BOT_TOKEN)" || exit $?
TAVILY_API_KEY="$(fetch_secret TAVILY_API_KEY)"       || exit $?
export DISCORD_BOT_TOKEN TAVILY_API_KEY
echo "resolved DISCORD_BOT_TOKEN (len=${#DISCORD_BOT_TOKEN}) + TAVILY_API_KEY (len=${#TAVILY_API_KEY}) from Bitwarden" >&2

if [ "$DRY_RUN" = "1" ]; then
  echo "dry-run: not touching docker" >&2
  exit 0
fi

# 3) (Re)create the stack. compose interpolates the two ${...} into the agent's env
#    from THIS shell only. host_uid/gid preserved (non-root, matches mounted files).
export HERMES_UID="${HERMES_UID:-$(id -u)}"
export HERMES_GID="${HERMES_GID:-$(id -g)}"
cd "$HERE"
docker compose -f "$COMPOSE_FILE" up -d
echo "locked stack up (agent token-injected from Bitwarden; bootstrap stayed on host)" >&2
