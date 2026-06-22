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

import re

# Token shapes to redact. Keep these broad enough to catch real credentials but
# anchored on a distinctive prefix so ordinary prose is not mangled.
GATEWAY_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_\-]{12,}\b"),       # OpenAI-style
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),             # GitHub PAT/OAuth
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{20,}\b"),           # Slack
    re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),                     # HuggingFace
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b"),               # GitLab PAT
    re.compile(r"\btvly-[A-Za-z0-9_\-]{16,}\b"),               # Tavily
    re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._\-]{20,}\b"),     # Bearer tokens
)


def redact_secrets(text: str) -> str:
    """Best-effort secret redaction before text can leave the gateway."""
    redacted = str(text or "")
    for pattern in GATEWAY_SECRET_PATTERNS:
        redacted = pattern.sub(
            lambda m: (m.group(1) if m.lastindex else "") + "[REDACTED]", redacted
        )
    return redacted
