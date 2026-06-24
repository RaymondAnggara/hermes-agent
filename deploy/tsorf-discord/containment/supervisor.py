#!/usr/bin/env python3
"""Phase 4 W3 / containment-(c): supervisor that holds the bootstrap secret so
the agent never does (PROTOTYPE).

Responsibility split (least privilege):
  - SUPERVISOR (this process): the ONLY thing that reads the raw model key, from
    one of two backends -- macOS Keychain (c3 bootstrap, present today) or
    Bitwarden Secrets Manager (c2 production target, via `bws`). It launches the
    egress proxy with the key in the PROXY's env only.
  - PROXY (model_key_proxy.py): holds the key in memory, injects it upstream.
  - AGENT (the Hermes gateway): started SEPARATELY, with NO model key in its env
    and its config base_url repointed at the proxy. Not started by this script
    -- the operator restarts the gateway during the gated cutover.

This is a PROTOTYPE for STOP GATE 4. It does not touch the live config or
restart the gateway. ``resolve_key`` is unit-tested with both backends mocked.

Backends:
  keychain : runs keychain_bootstrap.sh read   (c3)
  bitwarden: runs `bws secret get <id>` / `bws run` (c2). Needs BWS_ACCESS_TOKEN
             in the supervisor env (the ONE bootstrap token), itself ideally
             pinned in Keychain rather than plaintext.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_KEYCHAIN_SH = _HERE / "keychain_bootstrap.sh"


def resolve_key_keychain(*, _runner=subprocess.run) -> str:
    """Read the model key from the macOS Keychain via keychain_bootstrap.sh."""
    res = _runner(
        ["bash", str(_KEYCHAIN_SH), "read"],
        capture_output=True, text=True, check=True,
    )
    key = (res.stdout or "").strip()
    if not key:
        raise RuntimeError("keychain returned an empty model key")
    return key


def resolve_key_bitwarden(secret_id: str, *, _runner=subprocess.run) -> str:
    """Read the model key from Bitwarden Secrets Manager via `bws`.

    Requires BWS_ACCESS_TOKEN in the supervisor env. `bws secret get <id>`
    returns JSON with a `.value` field.
    """
    if not os.environ.get("BWS_ACCESS_TOKEN"):
        raise RuntimeError("BWS_ACCESS_TOKEN not set; cannot reach Bitwarden")
    res = _runner(
        ["bws", "secret", "get", secret_id, "--output", "json"],
        capture_output=True, text=True, check=True,
    )
    payload = json.loads(res.stdout)
    key = (payload.get("value") or "").strip()
    if not key:
        raise RuntimeError(f"bitwarden secret {secret_id} has no value")
    return key


def resolve_key(backend: str, *, bitwarden_secret_id: str | None = None,
                _runner=subprocess.run) -> str:
    if backend == "keychain":
        return resolve_key_keychain(_runner=_runner)
    if backend == "bitwarden":
        if not bitwarden_secret_id:
            raise RuntimeError("bitwarden backend needs --bitwarden-secret-id")
        return resolve_key_bitwarden(bitwarden_secret_id, _runner=_runner)
    raise RuntimeError(f"unknown backend: {backend!r}")


def child_env_for_proxy(key: str, base_env: dict | None = None) -> dict:
    """Build the env the PROXY subprocess runs with: the real key present,
    and -- defensively -- the agent's own model-key var scrubbed so a shared
    env can't leak it into the proxy's child processes."""
    env = dict(base_env if base_env is not None else os.environ)
    env.pop("OPENCODE_GO_API_KEY", None)  # the agent var must not ride along
    env["MODEL_PROXY_UPSTREAM_KEY"] = key
    return env


def launch_proxy(key: str, *, port: int, upstream: str,
                 _popen=subprocess.Popen) -> "subprocess.Popen":
    env = child_env_for_proxy(key)
    proxy_py = str(_HERE / "model_key_proxy.py")
    return _popen(
        [sys.executable, proxy_py, "--port", str(port), "--upstream", upstream],
        env=env,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="containment supervisor (prototype)")
    p.add_argument("--backend", choices=["keychain", "bitwarden"],
                   default="keychain")
    p.add_argument("--bitwarden-secret-id", default=None)
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--upstream", default="https://opencode.ai/zen/go/v1")
    p.add_argument("--dry-run", action="store_true",
                   help="resolve the key and confirm WITHOUT launching anything")
    args = p.parse_args(argv)

    key = resolve_key(args.backend, bitwarden_secret_id=args.bitwarden_secret_id)
    # Never print the key; confirm only its shape.
    print(f"resolved model key from {args.backend} "
          f"(len={len(key)}, prefix={key[:3]}…)", file=sys.stderr)
    if args.dry_run:
        print("dry-run: not launching proxy", file=sys.stderr)
        return 0

    proc = launch_proxy(key, port=args.port, upstream=args.upstream)
    print(f"proxy launched (pid={proc.pid}) on 127.0.0.1:{args.port}",
          file=sys.stderr)
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
