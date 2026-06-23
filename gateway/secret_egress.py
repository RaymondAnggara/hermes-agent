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
    re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_\-]{12,}\b"),       # OpenAI-style
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),             # GitHub PAT/OAuth
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{20,}\b"),           # Slack
    re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),                     # HuggingFace
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b"),               # GitLab PAT
    re.compile(r"\btvly-[A-Za-z0-9_\-]{16,}\b"),               # Tavily
    re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._\-]{20,}\b"),     # Bearer tokens
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
