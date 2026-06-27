"""Phase 5b secrets-hardening — invariants for the platform-token containment.

The DISCORD_BOT_TOKEN and TAVILY_API_KEY were moved OUT of the mounted plaintext
~/.hermes/.env (which the prompt-injectable agent can ``cat`` at /opt/data/.env)
and are now injected into the locked container's process env at launch by
deploy/.../network/launch_locked.sh, which fetches them from Bitwarden using a
bootstrap token that stays on the host.

These tests assert the SAFETY INVARIANTS of that wiring, not a frozen snapshot:
  * the locked compose must never carry a plaintext token VALUE;
  * token injection must be FAIL-CLOSED (compose refuses to render if a token is
    unset, so a bare ``docker compose up`` that skipped the launcher can't bring
    up a silently-tokenless bot);
  * the Bitwarden BOOTSTRAP token must never be wired into the container's env;
  * the launcher must source the bootstrap from Keychain and never echo a value.
"""

from __future__ import annotations

import re
import stat
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_NETWORK = (
    Path(__file__).resolve().parents[2]
    / "deploy" / "tsorf-discord" / "containment" / "network"
)
_COMPOSE = _NETWORK / "docker-compose.locked.yml"
_LAUNCHER = _NETWORK / "launch_locked.sh"

# The two platform tokens that this hardening governs.
_PLATFORM_TOKENS = ("DISCORD_BOT_TOKEN", "TAVILY_API_KEY")


def _agent_env_list() -> list[str]:
    data = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    return list(data["services"]["agent"].get("environment", []))


def _agent_env_value(name: str) -> str | None:
    for entry in _agent_env_list():
        if entry.startswith(f"{name}="):
            return entry.split("=", 1)[1]
    return None


@pytest.mark.parametrize("token", _PLATFORM_TOKENS)
def test_token_is_injected_by_interpolation_not_plaintext(token):
    """Each platform token must be a ``${TOKEN:?...}`` interpolation, never a
    literal value baked into the committed compose file."""
    value = _agent_env_value(token)
    assert value is not None, f"{token} missing from agent environment"
    # Must be a compose interpolation of the SAME-named var (value comes from the
    # launcher's shell), i.e. starts with ${TOKEN ...}.
    assert value.startswith("${" + token), (
        f"{token} must be injected via ${{{token}...}} interpolation, got {value!r}"
    )


@pytest.mark.parametrize("token", _PLATFORM_TOKENS)
def test_token_injection_is_fail_closed(token):
    """The ``:?`` form makes compose error out if the token is unset/empty, so a
    bare ``docker compose up`` (without the launcher) can't start a tokenless bot."""
    value = _agent_env_value(token)
    assert value is not None
    assert re.match(r"^\$\{" + re.escape(token) + r":\?", value), (
        f"{token} injection must be fail-closed (${{{token}:?...}}), got {value!r}"
    )


def test_no_plaintext_token_value_anywhere_in_compose():
    """No real-looking secret literal in the compose. The only credential-shaped
    string allowed is the explicit model-key PLACEHOLDER (W3)."""
    text = _COMPOSE.read_text(encoding="utf-8")
    # Discord bot tokens look like base64ish.base64ish.base64ish; Tavily keys are
    # tvly-...; reject either appearing as a literal value.
    assert not re.search(r"\btvly-[A-Za-z0-9_-]{8,}", text), "Tavily key literal leaked into compose"
    assert not re.search(r"\bMT[A-Za-z0-9]{20,}\.[A-Za-z0-9_-]{5,}\.", text), (
        "Discord token literal leaked into compose"
    )
    # The model-key var is allowed ONLY as the documented placeholder.
    model_val = _agent_env_value("OPENCODE_GO_API_KEY")
    assert model_val == "sk-LOCAL-PROXY-PLACEHOLDER", (
        f"model key must stay a placeholder, got {model_val!r}"
    )


def test_bootstrap_token_is_never_wired_into_the_container():
    """The Bitwarden bootstrap (BWS_ACCESS_TOKEN) must NOT appear in the agent's
    container env — it stays on the host launcher. Letting it into the
    prompt-injectable container would expose the WHOLE vault (incl. the model key
    we removed in W3), which is strictly worse than the plaintext-.env state."""
    env_text = "\n".join(_agent_env_list())
    assert "BWS_ACCESS_TOKEN" not in env_text
    # And not smuggled in via the whole compose either.
    assert "BWS_ACCESS_TOKEN" not in _COMPOSE.read_text(encoding="utf-8")


def test_launcher_exists_and_is_executable():
    assert _LAUNCHER.exists(), "launch_locked.sh missing"
    mode = _LAUNCHER.stat().st_mode
    assert mode & stat.S_IXUSR, "launch_locked.sh must be executable"


def test_launcher_sources_bootstrap_from_keychain_and_fetches_from_bitwarden():
    src = _LAUNCHER.read_text(encoding="utf-8")
    # Bootstrap comes from the Keychain item the model-key supervisor also uses.
    assert "hermes-bws-access-token" in src
    assert "keychain_bootstrap.sh" in src
    # Tokens are fetched from Bitwarden by NAME, not hardcoded values.
    for token in _PLATFORM_TOKENS:
        assert token in src
    assert "bws secret" in src


def test_launcher_never_echoes_a_secret_value():
    """The launcher must confirm only token LENGTHS / presence, never print a raw
    value to stdout/stderr (so logs/terminals don't capture a credential)."""
    src = _LAUNCHER.read_text(encoding="utf-8")
    # It reports lengths, which is the safe confirmation pattern.
    assert "len=${#DISCORD_BOT_TOKEN}" in src or "len=" in src
    # Crude but effective guard: no `echo $TOKEN` / `echo "$TOKEN"` of a secret.
    for token in _PLATFORM_TOKENS:
        assert not re.search(rf'echo\s+"?\${{?{token}}}?"?\s*$', src, re.MULTILINE), (
            f"launcher appears to echo the raw {token} value"
        )
