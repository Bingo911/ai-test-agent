"""Prompt/input sanitization before anything leaves the platform (§6.3, §14.4)."""

from __future__ import annotations

import re
from collections.abc import Iterable

from ..observability import redact

_TAG = re.compile(r"<[^>]{1,200}>")
_INJECTION = re.compile(
    r"(?:ignore (?:the )?(?:previous|above) instructions|忽略(?:之前|上述)指令|system prompt|do not follow|"
    r"expose cookies|导出\s*cookie|api[_-]?key|secretly|instead run|execute this code)",
    re.IGNORECASE,
)


def sanitize_text(value: str, *, known_secrets: Iterable[str] = (), max_chars: int = 6000) -> str:
    """Redact secret shapes plus declared values, strip markup, and neutralize instruction-looking text."""
    text = value if len(value) <= max_chars else value[:max_chars] + "...[truncated]"
    text = redact(text, tuple(known_secrets))
    text = _TAG.sub(" ", text)
    text = _INJECTION.sub("[redacted-instruction]", text)
    return " ".join(text.split())


def summarize_dom(html: str, *, known_secrets: Iterable[str] = (), max_chars: int = 4000) -> str:
    """Very small textual DOM summary; never send raw scripts, cookies or form values."""
    stripped = re.sub(r"(?is)<(script|style|noscript|template)[^>]*>.*?</\1>", " ", html)
    stripped = re.sub(
        r"(?i)<[^>]*(?:type=[\"']password[\"']|name=[\"'](?:otp|token|cc)[^\"']*[\"'])[^>]*>",
        " [masked-control] ",
        stripped,
    )
    text = _TAG.sub(" ", stripped)
    return sanitize_text(text, known_secrets=known_secrets, max_chars=max_chars)
