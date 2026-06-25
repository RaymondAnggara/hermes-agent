#!/usr/bin/env python3
"""Phase 4 W3 / containment-(c1): model-key egress proxy (PROTOTYPE).

The agent process must NOT hold the opencode-go / DeepSeek API key. Today the
key (``OPENCODE_GO_API_KEY``) is plaintext in ``~/.hermes/.env`` and lives in
the agent's own environment -- a prompt-injected agent could read and exfiltrate
it. This proxy moves the key OUT of the agent's blast radius:

    agent  --(no key, Authorization: Bearer sk-LOCAL-PLACEHOLDER)-->  this proxy
    proxy  --(injects the REAL key server-side)-->  https://opencode.ai/zen/go/v1

The agent talks to ``http://127.0.0.1:<port>``; the proxy is the ONLY process
that holds the real key (handed to it by the supervisor, which read it from the
macOS Keychain or Bitwarden -- see supervisor.py / keychain_bootstrap.sh). The
agent's config ``base_url`` is repointed at the proxy and its
``OPENCODE_GO_API_KEY`` is removed.

This is a PROTOTYPE for STOP GATE 4 review. It is NOT wired into the live
gateway yet -- the cutover (editing the runtime config.yaml / .env and
restarting the gateway) is a gated step the operator must approve. Nothing here
imports or mutates the running agent.

Design properties (all unit-tested in tests/gateway/test_phase4_w3_proxy.py):
  - The agent's inbound Authorization / x-api-key headers are DROPPED, never
    forwarded. The proxy injects its own. So even a misconfigured agent can't
    leak a different key upstream, and can't override the injected one.
  - The real key is never logged: request logging redacts it and never prints
    the Authorization header.
  - Hop-by-hop headers are stripped; the upstream Host is set correctly.
  - Responses (including SSE token streams) are streamed back chunk-by-chunk so
    interactive latency is unchanged.
  - Bind is 127.0.0.1 only (loopback) -- not reachable off-host.

Run standalone (the supervisor normally does this):
    MODEL_PROXY_UPSTREAM_KEY=sk-REAL... \
    python model_key_proxy.py --port 8787 \
        --upstream https://opencode.ai/zen/go/v1
"""

from __future__ import annotations

import argparse
import logging
import os
from urllib.parse import urlsplit

logger = logging.getLogger("model_key_proxy")

# Header name for the upstream credential. opencode-go is OpenAI-compatible:
# it authenticates with `Authorization: Bearer <key>`.
_AUTH_HEADER = "Authorization"

# Inbound credential headers we always DROP from the agent's request before
# forwarding (the agent must not be able to choose the upstream credential).
_DROP_INBOUND = {"authorization", "x-api-key", "api-key", "openai-api-key"}

# Hop-by-hop headers (RFC 7230 6.1) that must not be forwarded, plus Host /
# Content-Length which we recompute. We deliberately do NOT touch
# Accept-Encoding/Content-Encoding: the proxy relays compression transparently
# end-to-end (raw bytes both ways, auto_decompress=False), so the agent's own
# HTTP client negotiates and decodes exactly as it would talking direct. The
# upstream (opencode.ai zen/go relay) compresses regardless, so stripping the
# response Content-Encoding while passing compressed bytes corrupts the body.
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade",
    "host", "content-length",
}

# The placeholder the AGENT is configured with (so its config/.env contains no
# real secret). The proxy ignores its value entirely -- documented for clarity.
AGENT_PLACEHOLDER_KEY = "sk-LOCAL-PROXY-PLACEHOLDER"


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without binding a socket)
# ---------------------------------------------------------------------------

def build_upstream_url(upstream_base: str, incoming_path: str, query: str = "") -> str:
    """Join the configured upstream base with the agent's request path.

    ``upstream_base`` already includes the API path prefix (e.g.
    ``https://opencode.ai/zen/go/v1``); the agent calls e.g.
    ``/chat/completions``, which the OpenAI-style client builds by appending to
    the base. To avoid doubling the prefix we treat the agent's path as
    RELATIVE to the base path and join on a single slash.
    """
    base = upstream_base.rstrip("/")
    path = "/" + incoming_path.lstrip("/")
    # If the agent already included the base's path prefix, don't double it.
    base_path = urlsplit(base).path  # e.g. /zen/go/v1
    if base_path and path.startswith(base_path + "/"):
        path = path[len(base_path):]
    url = base + path
    if query:
        url += "?" + query
    return url


def rewrite_request_headers(
    incoming: dict[str, str], upstream_key: str, upstream_host: str
) -> dict[str, str]:
    """Return the header set to send upstream: agent credentials dropped,
    hop-by-hop dropped, real key injected, Host set to the upstream."""
    out: dict[str, str] = {}
    for name, value in incoming.items():
        lname = name.lower()
        if lname in _DROP_INBOUND or lname in _HOP_BY_HOP:
            continue
        out[name] = value
    out[_AUTH_HEADER] = f"Bearer {upstream_key}"
    out["Host"] = upstream_host
    return out


def redact_secret(text: str, secret: str) -> str:
    """Redact the real key from any string before it can be logged."""
    if secret and secret in text:
        text = text.replace(secret, "[REDACTED-MODEL-KEY]")
    return text


def safe_request_line(method: str, path: str) -> str:
    """A log line that can never contain the key (path only, no headers/body)."""
    # Defensive: strip any querystring that might carry a token.
    clean_path = path.split("?", 1)[0]
    return f"{method} {clean_path}"


# ---------------------------------------------------------------------------
# aiohttp app (the actual streaming reverse proxy)
# ---------------------------------------------------------------------------

def build_app(upstream_base: str, upstream_key: str):
    """Construct the aiohttp application. Imported lazily so the pure helpers
    above (and their tests) don't require aiohttp to be installed."""
    from aiohttp import web, ClientSession, ClientTimeout

    if not upstream_key:
        raise SystemExit(
            "refusing to start: no upstream key. Set MODEL_PROXY_UPSTREAM_KEY "
            "(the supervisor injects it from Keychain/Bitwarden)."
        )
    upstream_host = urlsplit(upstream_base).netloc

    async def health(_req):
        return web.json_response({"status": "ok", "upstream": upstream_host})

    async def proxy(request: "web.Request") -> "web.StreamResponse":
        url = build_upstream_url(upstream_base, request.match_info["tail"],
                                 request.query_string)
        headers = rewrite_request_headers(dict(request.headers), upstream_key,
                                          upstream_host)
        logger.info("→ %s", safe_request_line(request.method, request.path_qs))

        body = await request.read()
        session: ClientSession = request.app["session"]
        async with session.request(
            request.method, url, headers=headers, data=body or None,
            timeout=ClientTimeout(total=600),
        ) as upstream_resp:
            # Stream the response straight back (SSE-safe).
            resp = web.StreamResponse(status=upstream_resp.status)
            for k, v in upstream_resp.headers.items():
                # Relay everything except hop-by-hop. Content-Encoding is passed
                # through (raw bytes relayed verbatim) so the client decodes it.
                if k.lower() in _HOP_BY_HOP:
                    continue
                resp.headers[k] = v
            await resp.prepare(request)
            async for chunk in upstream_resp.content.iter_any():
                await resp.write(chunk)
            await resp.write_eof()
            return resp

    async def _on_startup(app):
        app["session"] = ClientSession(auto_decompress=False)

    async def _on_cleanup(app):
        await app["session"].close()

    app = web.Application()
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    app.router.add_get("/healthz", health)
    app.router.add_route("*", "/{tail:.*}", proxy)
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="model-key egress proxy")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (loopback only by default)")
    parser.add_argument("--upstream", default="https://opencode.ai/zen/go/v1")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    key = os.environ.get("MODEL_PROXY_UPSTREAM_KEY", "")
    from aiohttp import web
    app = build_app(args.upstream, key)
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        logger.warning("binding to non-loopback %s -- proxy is reachable "
                       "off-host; ensure a network egress control fronts it",
                       args.host)
    web.run_app(app, host=args.host, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
