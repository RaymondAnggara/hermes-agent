"""Outbound secret-egress redaction for the messaging gateway.

Single source of truth for the token-shaped patterns that must never leave the
gateway in a user-facing reply. This is *defense in depth* -- best-effort regex
redaction applied to outbound text on every platform's delivery path. It is NOT
a substitute for keeping secrets out of the agent's reach in the first place
(sandbox / secrets manager); a determined exfiltration can reshape a secret to
dodge a regex. It exists to catch the common case: the model echoing a
credential that appeared in the conversation.

Used by:
- ``gateway/platforms/base.py`` -- redacts every agent text reply before send.
- ``gateway/run.py`` -- Telegram provider-error / status paths.
"""

import os
import re

# --- Shape-based redaction -------------------------------------------------
# Token shapes to redact. Keep these broad enough to catch real credentials but
# anchored on a distinctive prefix so ordinary prose is not mangled. These
# catch credentials that appear in conversation *content* (e.g. a user pastes
# someone's GitHub token), including ones we never configured.
GATEWAY_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_\-]{12,}\b"),       # OpenAI-style (also covers
                                                                # the opencode-go key: it is
                                                                # an ``sk-`` 67-char token)
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),             # GitHub PAT/OAuth
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{20,}\b"),           # Slack
    re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),                     # HuggingFace
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b"),               # GitLab PAT
    re.compile(r"\btvly-[A-Za-z0-9_\-]{16,}\b"),               # Tavily
    re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._\-]{20,}\b"),     # Bearer tokens
    # --- Exchange / cloud credential shapes (Phase 4; Phase 5 #investment prep) ---
    # Distinctive prefixes / structures only, so ordinary prose is never mangled.
    # NOTE: prefix-less exchange keys (e.g. Binance's 64-char alphanumeric API
    # key/secret) are deliberately NOT shape-matched here -- a bare 64-char
    # alnum pattern false-positives on hashes/IDs. Those are covered instead by
    # the provider-agnostic VALUE layer below once configured as secret-named
    # env vars (BINANCE_API_SECRET, COINBASE_API_SECRET, ...).
    re.compile(r"-----BEGIN(?:[A-Z ]+)? PRIVATE KEY-----[\s\S]+?-----END(?:[A-Z ]+)? PRIVATE KEY-----"),  # PEM private key block (Coinbase CDP / SSH / EC / RSA)
    re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA)[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\borganizations/[0-9a-fA-F-]{8,}/apiKeys/[0-9a-fA-F-]{8,}\b"),  # Coinbase CDP key resource name
    re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}\b"),     # Stripe secret/restricted keys
)

# --- Value-based redaction (provider-agnostic) -----------------------------
# Scrub the *literal values* of the secrets THIS deployment actually holds,
# read from the environment by name. This protects whatever keys you configure
# (OPENCODE_GO_API_KEY, DISCORD_BOT_TOKEN, the next provider you switch to...)
# regardless of their format -- no pattern maintenance when you change keys.
_SECRET_ENV_NAME_RE = re.compile(
    r"(?:API_?KEY|ACCESS_?KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|AUTH|PRIVATE_?KEY)",
    re.IGNORECASE,
)
# Don't treat short values as secrets -- redacting a 4-char value everywhere
# would corrupt ordinary text. Real keys/tokens are comfortably longer.
_MIN_SECRET_VALUE_LEN = 12


def configured_secret_values(environ: "dict[str, str] | None" = None) -> list[str]:
    """Literal values of secret-named env vars, longest-first for exact-match
    redaction (longest first so a longer secret is scrubbed before a shorter
    one it might contain)."""
    env = os.environ if environ is None else environ
    values: set[str] = set()
    for name, value in env.items():
        if not value or len(value) < _MIN_SECRET_VALUE_LEN:
            continue
        if _SECRET_ENV_NAME_RE.search(name):
            values.add(value)
    return sorted(values, key=len, reverse=True)


def redact_secrets(text: str, *, environ: "dict[str, str] | None" = None) -> str:
    """Best-effort secret redaction before text can leave the gateway.

    Two complementary layers: (1) exact configured secret VALUES — provider-
    agnostic, covers whatever keys this deployment holds; (2) known token
    SHAPES — covers arbitrary credentials that appear in message content.
    Defense in depth, not a substitute for keeping secrets out of the agent's
    reach (sandbox / secrets manager).
    """
    redacted = str(text or "")
    if not redacted:
        return redacted
    # 1. Exact configured secret values.
    for value in configured_secret_values(environ):
        if value in redacted:
            redacted = redacted.replace(value, "[REDACTED]")
    # 2. Shape-based patterns.
    for pattern in GATEWAY_SECRET_PATTERNS:
        redacted = pattern.sub(
            lambda m: (m.group(1) if m.lastindex else "") + "[REDACTED]", redacted
        )
    return redacted
