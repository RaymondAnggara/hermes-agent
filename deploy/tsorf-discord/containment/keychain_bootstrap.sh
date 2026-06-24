#!/usr/bin/env bash
# Phase 4 W3 / containment-(c3): macOS Keychain bootstrap for the model key.
#
# Keeps the bootstrap secret OUT of plaintext ~/.hermes/.env. The macOS Keychain
# is the local secret store that is already present on this host (Bitwarden's
# `bws` is the production target in supervisor.py; this is the zero-dependency
# bootstrap path the operator can use immediately).
#
# The key is read by the SUPERVISOR (a separate process from the agent), which
# injects it ONLY into the proxy subprocess env. The prompt-injectable agent
# never has Keychain access to this item and never sees the raw key.
#
# Usage:
#   ./keychain_bootstrap.sh store         # prompts (no echo) and stores the key
#   ./keychain_bootstrap.sh store-stdin   # reads key from stdin (for piping)
#   ./keychain_bootstrap.sh read          # prints the key to stdout (supervisor)
#   ./keychain_bootstrap.sh delete        # removes the item
#   ./keychain_bootstrap.sh check         # exit 0 if present, 1 if not (no print)
#
# Item identity (account/service) is fixed so all three tools agree.
set -euo pipefail

SERVICE="hermes-tsorf-model-key"
ACCOUNT="opencode-go"

cmd="${1:-}"
case "$cmd" in
  store)
    # -w with no value makes `security` prompt interactively without echo.
    security add-generic-password -U -s "$SERVICE" -a "$ACCOUNT" -w
    echo "stored model key in Keychain (service=$SERVICE account=$ACCOUNT)" >&2
    ;;
  store-stdin)
    KEY="$(cat)"
    if [ -z "$KEY" ]; then echo "no key on stdin" >&2; exit 2; fi
    security add-generic-password -U -s "$SERVICE" -a "$ACCOUNT" -w "$KEY"
    echo "stored model key from stdin" >&2
    ;;
  read)
    # -w prints only the password to stdout. Supervisor captures this.
    security find-generic-password -s "$SERVICE" -a "$ACCOUNT" -w
    ;;
  check)
    if security find-generic-password -s "$SERVICE" -a "$ACCOUNT" -w >/dev/null 2>&1; then
      echo "present" >&2; exit 0
    else
      echo "absent" >&2; exit 1
    fi
    ;;
  delete)
    security delete-generic-password -s "$SERVICE" -a "$ACCOUNT" >/dev/null
    echo "deleted" >&2
    ;;
  *)
    echo "usage: $0 {store|store-stdin|read|check|delete}" >&2
    exit 64
    ;;
esac
