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
    # Phase 4 exchange/cloud credential shapes:
    _tok("sk-", "UbL" + "a" * 40 + "-" + "b" * 20),  # opencode-go key shape (sk-, ~67 chars)
    _tok("AKIA", "IOSFODNN7EXAMPLE"),                # AWS access key id
    _tok("ASIA", "IOSFODNN7EXAMPLE"),                # AWS temporary access key id
    _tok("sk_live_", "abcdef0123456789ABCDEF"),      # Stripe secret key
    _tok("rk_test_", "abcdef0123456789ABCDEF"),      # Stripe restricted key
    _tok("organizations/", "abc12345/apiKeys/def67890abcdef"),  # Coinbase CDP key path
])
def test_known_token_shapes_are_redacted(secret):
    out = redact_secrets(f"the key is {secret} ok")
    assert secret not in out
    assert "[REDACTED]" in out


def test_pem_private_key_block_is_fully_redacted():
    """A pasted PEM private key (Coinbase CDP / SSH / EC) is scrubbed whole,
    not just its header line -- the body is the secret."""
    pem = (
        "-----BEGIN EC PRIVATE KEY-----\n"
        "MHcCAQEEIabc123def456ghi789jkl012mno345pqr678stu901\n"
        "-----END EC PRIVATE KEY-----"
    )
    out = redact_secrets(f"here is the key:\n{pem}\nthanks")
    assert "MHcCAQEEI" not in out
    assert "[REDACTED]" in out


def test_exchange_shape_patterns_do_not_mangle_prose():
    """The new exchange/cloud patterns must not fire on ordinary text."""
    for text in (
        "The organization apiKeys are reviewed by the platform team.",
        "AKIA is a common AWS prefix but AKIA alone is not a key.",
        "commit e1f49fd touched the gateway module.",
    ):
        assert redact_secrets(text) == text


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


# --- value-based (provider-agnostic) redaction ---------------------------

def test_configured_value_redacted_regardless_of_shape():
    """A provider key with NO recognizable prefix is still scrubbed by value --
    this is what makes switching providers safe without pattern updates."""
    env = {"SOME_NEW_PROVIDER_API_KEY": "weirdformat-no-known-prefix-9999xyz"}
    out = redact_secrets("the model key is weirdformat-no-known-prefix-9999xyz here", environ=env)
    assert "weirdformat-no-known-prefix-9999xyz" not in out
    assert "[REDACTED]" in out


def test_discord_bot_token_value_redacted():
    env = {"DISCORD_BOT_TOKEN": "MzkyToKeNvaLue.Xq1234.abcDEFghiJKLmnoPQRstuVWXyz0"}
    out = redact_secrets("token: MzkyToKeNvaLue.Xq1234.abcDEFghiJKLmnoPQRstuVWXyz0", environ=env)
    assert "MzkyToKeNvaLue" not in out
    assert "[REDACTED]" in out


def test_short_value_is_not_redacted():
    """A short value must not be scrubbed -- would corrupt ordinary text."""
    env = {"X_TOKEN": "yes"}
    assert redact_secrets("the answer is yes indeed", environ=env) == "the answer is yes indeed"


def test_non_secret_env_name_is_not_redacted():
    """Only secret-NAMED env vars are treated as values to scrub."""
    env = {"HERMES_HOME": "/Users/someone/.hermes/private"}
    text = "files live under /Users/someone/.hermes/private today"
    assert redact_secrets(text, environ=env) == text
