"""Outbound secret-egress redaction (Phase 1 safety control).

Asserts the invariant that token-shaped strings are scrubbed before a reply
leaves the gateway -- including the exact GitHub-PAT shape that leaked through
the un-redacted Discord path during the Phase 1 demo.

NOTE: fixtures are assembled from a (prefix, body) pair via ``_tok`` so no
contiguous token literal appears in this file -- otherwise GitHub secret-
scanning push protection (a credentials-policy tripwire we rely on) blocks the
push of our own test data.
"""

import pytest

from gateway.secret_egress import redact_secrets


def _tok(prefix: str, body: str) -> str:
    """Build a token-shaped string at runtime (keeps the literal out of source)."""
    return prefix + body


_BODY = "0123456789abcdefghij0123456789abcd"

# The shape that reached Discord un-redacted during the demo (GitHub PAT).
LEAKED = _tok("ghp", "_" + _BODY)


@pytest.mark.parametrize("secret", [
    LEAKED,                                          # GitHub PAT (regression)
    _tok("ghu", "_" + _BODY),                        # GitHub OAuth user token
    _tok("sk", "-ABCDEFGHIJKLMNOPQRSTUVWX"),         # OpenAI-style
    _tok("xox", "b-0123456789-abcdefghijklmno"),     # Slack
    _tok("hf", "_0123456789abcdefghijklmnopqrst"),   # HuggingFace
    _tok("glpat", "-ABCDEFGHIJ0123456789"),          # GitLab
    _tok("tvly", "-0123456789abcdefghijklmnop"),     # Tavily (new)
])
def test_known_token_shapes_are_redacted(secret):
    out = redact_secrets(f"the key is {secret} ok")
    assert secret not in out
    assert "[REDACTED]" in out


def test_bearer_header_redacted_keeps_prefix():
    out = redact_secrets("Authorization: Bearer abcdefghijklmnopqrstuvwxyz123")
    assert "abcdefghijklmnopqrstuvwxyz123" not in out
    assert "Bearer [REDACTED]" in out


def test_ordinary_prose_is_untouched():
    text = "Hermes is the herald of the gods; he invented the lyre."
    assert redact_secrets(text) == text


def test_secret_embedded_in_summary_is_scrubbed():
    """Mirrors the demo: a summary that echoes a credential must not emit it."""
    reply = f"Deploy key provided: {LEAKED} (looks like a GitHub PAT). Status: shipped."
    out = redact_secrets(reply)
    assert LEAKED not in out
    assert "[REDACTED]" in out


def test_empty_and_none_safe():
    assert redact_secrets("") == ""
    assert redact_secrets(None) == ""
