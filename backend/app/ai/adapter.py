"""Provider-neutral AI adapter (§6.3, §14.4) for an OpenAI-compatible chat completions endpoint."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import Settings
from ..observability import get_logger, redact

log = get_logger(__name__)


class AiError(Exception):
    code = "AI_UNAVAILABLE"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        self.message = message
        self.details = details or {}
        super().__init__(message)


class AiUnavailable(AiError):
    code = "AI_UNAVAILABLE"


class AiBudgetExceeded(AiError):
    code = "AI_BUDGET_EXCEEDED"


@dataclass
class AiUsage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    model: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)

    def merge(self, other: AiUsage) -> None:
        self.calls += other.calls
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.latency_ms += other.latency_ms
        self.model = other.model or self.model
        self.events.extend(other.events)

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "latency_ms": self.latency_ms,
            "model": self.model,
        }


@dataclass
class AiCall:
    content: str
    usage: AiUsage


class AiAdapter:
    """One adapter for compile/vision/analysis. Calls are bounded and never trusted."""

    def __init__(self, settings: Settings, *, purpose: str = "compiler", timeout_seconds: float | None = None) -> None:
        self.settings = settings
        self.purpose = purpose
        self.base_url = (settings.ai_base_url or "").rstrip("/")
        self.timeout = timeout_seconds or settings.ai_timeout_seconds
        self.max_calls = settings.ai_compiler_max_calls if purpose == "compiler" else settings.ai_max_calls_per_run
        self.max_total_ms = settings.ai_compiler_max_total_ms if purpose == "compiler" else settings.ai_max_total_ms
        self.usage = AiUsage(model=settings.ai_model)
        self._json_supported = True

    @property
    def enabled(self) -> bool:
        return bool(self.settings.ai_enabled and self.base_url)

    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.ai_api_key:
            headers["Authorization"] = f"Bearer {self.settings.ai_api_key}"
        return headers

    def chat_json(
        self, messages: list[dict[str, Any]], *, temperature: float = 0.0, max_tokens: int | None = None
    ) -> AiCall:
        if not self.enabled:
            raise AiUnavailable("AI provider is disabled or not configured")
        if self.usage.calls >= self.max_calls:
            raise AiBudgetExceeded(f"AI call budget exhausted for this task ({self.max_calls} calls)")
        elapsed = self.usage.latency_ms
        if elapsed >= self.max_total_ms:
            raise AiBudgetExceeded(f"AI time budget exhausted for this task ({self.max_total_ms} ms)")
        payload: dict[str, Any] = {
            "model": self.settings.ai_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens or self.settings.ai_max_output_tokens,
        }
        if self._json_supported:
            payload["response_format"] = {"type": "json_object"}
        started = time.monotonic()
        body = self._post(payload)
        latency = int((time.monotonic() - started) * 1000)
        usage = body.get("usage") or {}
        choices = body.get("choices") or []
        if not choices:
            raise AiUnavailable("AI provider returned no completion", details={"finish": body.get("finish_reason")})
        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        call_usage = AiUsage(
            calls=1,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            latency_ms=latency,
            model=str(body.get("model") or self.settings.ai_model),
            events=[{"purpose": self.purpose, "latency_ms": latency, "tokens": int(usage.get("total_tokens") or 0)}],
        )
        self.usage.merge(call_usage)
        return AiCall(content=content, usage=call_usage)

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/chat/completions"
        attempts = 2
        # A default rather than None: every exit of this loop raises, and `raise None` would be a TypeError.
        last_error: Exception = AiUnavailable("The AI provider completed no attempt")
        for attempt in range(attempts):
            try:
                with httpx.Client(timeout=httpx.Timeout(self.timeout, connect=min(10.0, self.timeout))) as client:
                    response = client.post(url, json=payload, headers=self.headers())
                if response.status_code in (400, 422) and "response_format" in payload and self._json_supported:
                    # provider rejects strict json mode; retry once without it
                    self._json_supported = False
                    payload = {key: value for key, value in payload.items() if key != "response_format"}
                    continue
                if response.status_code in (408, 429, 500, 502, 503, 504):
                    last_error = AiUnavailable(f"AI provider returned HTTP {response.status_code}")
                elif response.status_code >= 400:
                    raise AiUnavailable(
                        f"AI provider returned HTTP {response.status_code}: {redact(response.text[:400])}",
                        details={"status": response.status_code},
                    )
                else:
                    parsed = response.json()
                    if isinstance(parsed, dict):
                        return parsed
                    raise AiUnavailable("AI provider returned a non-object response")
            except (httpx.HTTPError, ValueError) as exc:
                last_error = AiUnavailable(f"AI provider is unreachable: {type(exc).__name__}")
            if attempt + 1 < attempts:
                time.sleep(0.4 * (attempt + 1))
        raise last_error

    def complete_json(self, raw: str) -> dict[str, Any]:
        """Parse a model reply into an object, tolerating code fences and leading prose."""
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3]
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise AiUnavailable("AI provider did not return a JSON object", details={"preview": redact(text[:200])})
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise AiUnavailable("AI provider returned malformed JSON", details={"error": str(exc)}) from exc
        if not isinstance(payload, dict):
            raise AiUnavailable("AI provider returned a non-object JSON value")
        return payload


def budget_from(settings: Settings, purpose: str, *, calls: int, total_ms: int) -> AiAdapter:
    adapter = AiAdapter(settings, purpose=purpose)
    adapter.max_calls = calls
    adapter.max_total_ms = total_ms
    return adapter
