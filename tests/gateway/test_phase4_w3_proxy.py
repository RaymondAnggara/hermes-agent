"""Phase 4 W3 / containment-(c) prototype tests: model-key egress proxy +
supervisor.

These prove the containment property the prompt demands -- "no plaintext secret
the agent can cat" / the model key leaves the agent's blast radius -- as
INFRASTRUCTURE, not prose:

  - The proxy DROPS the agent's inbound credential headers and injects the real
    key server-side, so the agent never holds (and cannot override) it. Proven
    end-to-end against a real stub upstream, including that a streamed (SSE)
    response is relayed intact.
  - The real key is never logged.
  - The supervisor reads the key from Keychain (c3) or Bitwarden (c2) and builds
    a proxy env that contains the key but scrubs the agent's own model-key var.

The proxy/supervisor live under deploy/tsorf-discord/containment/ (deploy
tooling, not core). They are loaded by path so the tests don't depend on that
directory being importable as a package. This is a GATED prototype -- nothing
here touches the live gateway config.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Load the deploy-side modules by path (they're not on sys.path as a package).
# ---------------------------------------------------------------------------
_CONTAINMENT = (
    Path(__file__).resolve().parents[2]
    / "deploy" / "tsorf-discord" / "containment"
)


def _load(modname: str, filename: str):
    spec = importlib.util.spec_from_file_location(
        f"_w3_{modname}", _CONTAINMENT / filename
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


proxy = _load("proxy", "model_key_proxy.py")
supervisor = _load("supervisor", "supervisor.py")

REAL_KEY = "sk-" + "R" * 48  # stand-in for the real opencode-go key


# ---------------------------------------------------------------------------
# Pure helpers: header rewrite is where the credential boundary lives
# ---------------------------------------------------------------------------

def test_inbound_agent_credentials_are_dropped_and_real_key_injected():
    """The agent sends a placeholder (or even a stolen) Authorization; the proxy
    must DROP it and inject its own. The injected key wins unconditionally."""
    incoming = {
        "Authorization": f"Bearer {proxy.AGENT_PLACEHOLDER_KEY}",
        "X-Api-Key": "agent-tried-to-sneak-this",
        "Content-Type": "application/json",
        "Connection": "keep-alive",      # hop-by-hop, must drop
        "Host": "127.0.0.1:8787",        # must be rewritten
        "Accept-Encoding": "gzip, br",    # passed through (transparent compression)
    }
    out = proxy.rewrite_request_headers(incoming, REAL_KEY, "opencode.ai")

    assert out["Authorization"] == f"Bearer {REAL_KEY}"
    assert proxy.AGENT_PLACEHOLDER_KEY not in json.dumps(out)
    assert "X-Api-Key" not in out and "x-api-key" not in {k.lower() for k in out}
    assert "agent-tried-to-sneak-this" not in json.dumps(out)
    assert "Connection" not in out
    # Accept-Encoding is relayed (raw-bytes passthrough) so the client decodes
    # the upstream's compressed response exactly as it would talking direct.
    assert out["Accept-Encoding"] == "gzip, br"
    assert out["Host"] == "opencode.ai"
    assert out["Content-Type"] == "application/json"  # benign headers pass


def test_build_upstream_url_does_not_double_the_base_path():
    base = "https://opencode.ai/zen/go/v1"
    # OpenAI client appends /chat/completions to the base it was given (the
    # proxy URL). The proxy must produce exactly one base path prefix.
    assert proxy.build_upstream_url(base, "/chat/completions") == \
        "https://opencode.ai/zen/go/v1/chat/completions"
    # If the agent already carried the prefix, don't double it.
    assert proxy.build_upstream_url(base, "/zen/go/v1/models") == \
        "https://opencode.ai/zen/go/v1/models"
    # Query string preserved.
    assert proxy.build_upstream_url(base, "/models", "limit=5") == \
        "https://opencode.ai/zen/go/v1/models?limit=5"


def test_real_key_is_never_emitted_in_logs():
    assert proxy.redact_secret(f"oops {REAL_KEY} leaked", REAL_KEY) == \
        "oops [REDACTED-MODEL-KEY] leaked"
    # The request log line carries only method + path (never headers/body), and
    # strips any querystring that could carry a token.
    line = proxy.safe_request_line("POST", "/chat/completions?token=" + REAL_KEY)
    assert REAL_KEY not in line
    assert line == "POST /chat/completions"


def test_build_app_refuses_to_start_without_a_key():
    with pytest.raises(SystemExit):
        proxy.build_app("https://opencode.ai/zen/go/v1", "")


# ---------------------------------------------------------------------------
# Supervisor: reads the key from a backend, never exposes it to the agent
# ---------------------------------------------------------------------------

def test_resolve_key_keychain_reads_via_helper_script():
    runner = MagicMock(return_value=MagicMock(stdout=REAL_KEY + "\n"))
    key = supervisor.resolve_key_keychain(_runner=runner)
    assert key == REAL_KEY
    # It invoked the keychain bootstrap script's `read` verb.
    argv = runner.call_args.args[0]
    assert argv[0] == "bash" and argv[-1] == "read"


def test_resolve_key_keychain_rejects_empty():
    runner = MagicMock(return_value=MagicMock(stdout="\n"))
    with pytest.raises(RuntimeError):
        supervisor.resolve_key_keychain(_runner=runner)


def test_resolve_key_bitwarden_parses_value(monkeypatch):
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "boot-token")
    payload = json.dumps({"id": "abc", "key": "model", "value": REAL_KEY})
    runner = MagicMock(return_value=MagicMock(stdout=payload))
    key = supervisor.resolve_key_bitwarden("abc-secret-id", _runner=runner)
    assert key == REAL_KEY
    argv = runner.call_args.args[0]
    assert argv[:3] == ["bws", "secret", "get"]


def test_resolve_key_bitwarden_requires_bootstrap_token(monkeypatch):
    monkeypatch.delenv("BWS_ACCESS_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="BWS_ACCESS_TOKEN"):
        supervisor.resolve_key_bitwarden("abc", _runner=MagicMock())


def test_resolve_key_dispatch_rejects_unknown_backend():
    with pytest.raises(RuntimeError, match="unknown backend"):
        supervisor.resolve_key("smoke-signals")


def test_proxy_child_env_holds_key_but_scrubs_agent_var():
    """The proxy subprocess gets MODEL_PROXY_UPSTREAM_KEY, and the agent's own
    OPENCODE_GO_API_KEY is removed so it can't ride along into the proxy's
    children. (The AGENT process, started separately, has neither the proxy var
    nor -- post-cutover -- the real key.)"""
    base = {"PATH": "/usr/bin", "OPENCODE_GO_API_KEY": "leftover-real-key",
            "HOME": "/Users/x"}
    env = supervisor.child_env_for_proxy(REAL_KEY, base_env=base)
    assert env["MODEL_PROXY_UPSTREAM_KEY"] == REAL_KEY
    assert "OPENCODE_GO_API_KEY" not in env
    assert env["PATH"] == "/usr/bin"  # unrelated env preserved


# ---------------------------------------------------------------------------
# End-to-end: real proxy app over a stub upstream (test the map, don't trust it)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_proxy_injects_key_and_streams_response_end_to_end():
    """Spin up a stub 'upstream', point the real proxy app at it, and drive a
    streamed POST through the proxy. Assert (a) the upstream saw the REAL key,
    not the agent's placeholder, and (b) a chunked/streamed body is relayed
    intact.

    Built with aiohttp.test_utils directly so we don't depend on the optional
    pytest-aiohttp fixtures."""
    from aiohttp import web
    from aiohttp.test_utils import TestServer, TestClient

    seen = {}

    async def upstream_handler(request):
        seen["auth"] = request.headers.get("Authorization")
        seen["had_xapikey"] = "X-Api-Key" in request.headers
        seen["path"] = request.path
        seen["body"] = await request.text()
        resp = web.StreamResponse(status=200,
                                  headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        for chunk in (b"data: hello\n\n", b"data: world\n\n", b"data: [DONE]\n\n"):
            await resp.write(chunk)
        await resp.write_eof()
        return resp

    upstream = web.Application()
    upstream.router.add_route("*", "/{tail:.*}", upstream_handler)
    up_server = TestServer(upstream)
    await up_server.start_server()
    upstream_base = f"http://127.0.0.1:{up_server.port}"

    app = proxy.build_app(upstream_base, REAL_KEY)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post(
            "/chat/completions",
            data=b'{"model":"deepseek-v4-flash","stream":true}',
            headers={
                "Authorization": f"Bearer {proxy.AGENT_PLACEHOLDER_KEY}",
                "X-Api-Key": "agent-sneak",
                "Content-Type": "application/json",
            },
        )
        body = await resp.text()
    finally:
        await client.close()
        await up_server.close()

    assert resp.status == 200
    # Upstream received the REAL key, injected by the proxy...
    assert seen["auth"] == f"Bearer {REAL_KEY}"
    # ...and NOT the agent's placeholder or sneaked key.
    assert proxy.AGENT_PLACEHOLDER_KEY not in (seen["auth"] or "")
    assert seen["had_xapikey"] is False
    assert seen["path"] == "/chat/completions"
    assert seen["body"] == '{"model":"deepseek-v4-flash","stream":true}'
    # The streamed SSE body was relayed intact.
    assert "data: hello" in body and "data: world" in body
    assert "data: [DONE]" in body


@pytest.mark.asyncio
async def test_proxy_relays_compressed_upstream_transparently():
    """Regression for the live-cutover finding: the opencode.ai zen/go relay
    gzips responses regardless of Accept-Encoding. The proxy must relay the
    Content-Encoding header + raw compressed bytes verbatim so the client
    decodes them -- it must NOT strip Content-Encoding while passing compressed
    bytes (which corrupts the body). Proven with a real gzip stub upstream."""
    import gzip
    from aiohttp import web
    from aiohttp.test_utils import TestServer, TestClient

    original = '{"choices":[{"message":{"content":"pong"}}]}'

    async def gzip_upstream(request):
        body = gzip.compress(original.encode())
        return web.Response(body=body, headers={
            "Content-Encoding": "gzip", "Content-Type": "application/json",
        })

    up = web.Application()
    up.router.add_route("*", "/{tail:.*}", gzip_upstream)
    up_server = TestServer(up)
    await up_server.start_server()

    app = proxy.build_app(f"http://127.0.0.1:{up_server.port}", REAL_KEY)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        # aiohttp's client auto-decompresses based on Content-Encoding, exactly
        # like the agent's real HTTP client -- so a correct passthrough yields
        # the original JSON.
        resp = await client.post("/chat/completions",
                                 headers={"Authorization": "Bearer placeholder"})
        text = await resp.text()
    finally:
        await client.close()
        await up_server.close()

    assert resp.status == 200
    assert text == original
