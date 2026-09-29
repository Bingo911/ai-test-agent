"""Structured logging with correlation fields and secret redaction (§15.4, §14.3)."""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from typing import Any

current_request: ContextVar[str] = ContextVar("request_id", default="-")
current_execution: ContextVar[str] = ContextVar("execution_id", default="-")
current_step: ContextVar[str] = ContextVar("step_id", default="-")
current_tenant: ContextVar[str] = ContextVar("tenant_id", default="-")

_CORRELATION = {
    "request_id": current_request,
    "tenant_id": current_tenant,
    "execution_id": current_execution,
    "step_id": current_step,
}

_QUERY_SECRET = re.compile(
    r"([?&](?:password|passwd|pwd|token|secret|api[_-]?key|otp|code|authorization)=)([^&#\s]*)",
    re.IGNORECASE,
)
_HEADER_SECRET = re.compile(r'("(?:authorization|cookie|set-cookie|password|otp|token)"\s*:\s*")[^"]*(")', re.I)
_BARE_SECRET = re.compile(
    r"\b((?:password|passwd|pwd|secret|token|otp|api_key|apikey)[\"']?\s*[:=]\s*)[\"']?[^\"',\s&]+",
    re.IGNORECASE,
)
_LONG_TOKEN = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_.-]+\b|\bBearer\s+[A-Za-z0-9._~+/-]{12,}", re.I)

REDACTED = "***"


def redact(text: str, extra_values: tuple[str, ...] = ()) -> str:
    """Mask known secret shapes plus any values the caller declares sensitive."""
    result = text
    for value in extra_values:
        if value and len(value) >= 3:
            result = result.replace(value, REDACTED)
    result = _QUERY_SECRET.sub(lambda m: m.group(1) + REDACTED, result)
    result = _HEADER_SECRET.sub(lambda m: m.group(1) + REDACTED + m.group(2), result)
    result = _BARE_SECRET.sub(lambda m: m.group(1) + REDACTED, result)
    return _LONG_TOKEN.sub(REDACTED, result)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": redact(str(record.getMessage())),
        }
        for key, var in _CORRELATION.items():
            payload[key] = var.get()
        extras = getattr(record, "fields", None)
        if isinstance(extras, dict):
            payload.update(
                {
                    key: (redact(str(value)) if isinstance(value, str) else value)
                    for key, value in extras.items()
                    if key not in {"secret", "password", "value", "storage_state"}
                }
            )
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(RedactingFormatter() if json_output else logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "urllib3", "botocore", "asyncio", "multipart"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
