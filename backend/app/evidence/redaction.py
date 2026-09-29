"""Evidence sanitization (§12.1, §14.3).

Screenshots, DOM snapshots and network rows all come out of a page the *target* controls, so they
are treated as untrusted content: secrets are masked, scripts are removed, and nothing is ever
re-served into the console origin without passing through here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

from ..observability import redact

SECRET_PARAM_NAMES = re.compile(
    r"(?i)(pass|pwd|secret|token|otp|mfa|auth|cookie|session|apikey|api_key|access[_-]?key|signature|credit|cvv|ssn|card)"
)
SECRET_ATTR_NAMES = re.compile(
    r"(?i)^(value|srcdoc|data-[a-z-]*(?:token|secret|password|api[_-]?key)|(?:aria-[a-z-]*)?value)$"
)
DROP_TAGS = ("script", "style", "noscript", "template", "iframe", "object", "embed", "link", "meta", "svg")

_DROP_ALTERNATION = "|".join(DROP_TAGS)
_DROP_BLOCK = re.compile(rf"(?is)<({_DROP_ALTERNATION})\b[^>]*>.*?</\1\s*>")
_DROP_SELF_CLOSING = re.compile(rf"(?is)<({_DROP_ALTERNATION})\b[^>]*/?>")
_EVENT_ATTR = re.compile(r"(?is)\s+on[a-z]{3,20}\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)")
_TAG_WITH_ATTRS = re.compile(r"(?is)<([a-z][a-z0-9-]*)\b([^>]*)>")
_ATTR_PAIR = re.compile(r"(?is)([a-z_:][-a-z0-9_:.]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s\"'>]+)")

#: Runs inside the page before `content()` is taken, so the snapshot never contains live scripts.
IN_PAGE_DOM_SCRUB = """
() => {
  const drop = ['script', 'style', 'noscript', 'template', 'svg', 'link', 'meta'];
  for (const name of drop) document.querySelectorAll(name).forEach((node) => node.remove());
  for (const node of document.querySelectorAll('*')) {
    for (const attribute of [...node.attributes]) {
      const name = attribute.name.toLowerCase();
      if (name.startsWith('on')) node.removeAttribute(attribute.name);
      if (name === 'srcdoc') node.removeAttribute(attribute.name);
    }
    if (node instanceof HTMLInputElement) {
      if (node.type === 'password' || node.type === 'hidden') node.value = '';
    } else if (node instanceof HTMLTextAreaElement) {
      if (node.name && /pass|pwd|secret|token|otp/i.test(node.name)) node.value = '';
    }
  }
  return document.documentElement ? document.documentElement.outerHTML : '';
}
"""


def redact_url(url: str, *, known_secrets: Iterable[str] = ()) -> str:
    """Keep the shape of a URL but drop credentials and mask secret-looking query parameters."""
    if not url:
        return ""
    text = redact(url, tuple(known_secrets))
    try:
        parts = urlsplit(text)
    except ValueError:
        return redact(text)[:500]
    netloc = parts.netloc
    if "@" in netloc:  # user:pass@host
        netloc = "***@" + netloc.rsplit("@", 1)[1]
    query_items = []
    for chunk in parts.query.split("&") if parts.query else []:
        if not chunk:
            continue
        name, _, value = chunk.partition("=")
        query_items.append(f"{name}=***" if SECRET_PARAM_NAMES.search(name) or value else f"{name}=")
    query = "&".join(item for item in query_items if "=" in item)
    path = parts.path if SECRET_PARAM_NAMES.search(parts.path) is None else _mask_path(parts.path)
    return urlunsplit((parts.scheme, netloc, path, query, "")).rstrip("?")[:500]


def _mask_path(path: str) -> str:
    segments = path.split("/")
    return "/".join("***" if segment and SECRET_PARAM_NAMES.search(segment) else segment for segment in segments)


def _clean_attribute(name: str, value: str, *, known_secrets: tuple[str, ...]) -> tuple[str, str] | None:
    """Return the attribute to keep, or None to drop it entirely."""
    lowered = name.lower()
    if lowered.startswith("on") or lowered in {"srcdoc", "style", "formaction"}:
        return None
    if SECRET_ATTR_NAMES.match(lowered):
        return (name, "***")
    if lowered in {"href", "src", "action", "poster", "data"}:
        return (name, f'"{redact_url(value, known_secrets=known_secrets)}"')
    cleaned = redact(value, known_secrets)
    return (name, f'"{cleaned[:500]}"')


def sanitize_dom(html: str, *, known_secrets: Iterable[str] = (), max_bytes: int = 2 * 1024 * 1024) -> str:
    """Backstop scrub applied to any DOM text we are about to store, whatever produced it."""
    secrets = tuple(item for item in known_secrets if item)
    text = _DROP_BLOCK.sub(" ", html)
    text = _DROP_SELF_CLOSING.sub(" ", text)
    text = _EVENT_ATTR.sub("", text)

    def rewrite_tag(match: re.Match[str]) -> str:
        tag, raw_attrs = match.group(1), match.group(2)
        kept: list[str] = []
        for attribute in _ATTR_PAIR.finditer(raw_attrs):
            cleaned = _clean_attribute(attribute.group(1), _bare(attribute.group(2)), known_secrets=secrets)
            if cleaned is not None:
                kept.append(f"{cleaned[0]}={cleaned[1]}" if cleaned[1] != "***" else f'{cleaned[0]}="***"')
        return f"<{tag}{' ' + ' '.join(kept) if kept else ''}>"

    text = _TAG_WITH_ATTRS.sub(rewrite_tag, text)
    text = redact(text, secrets)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) > max_bytes:
        text = encoded[:max_bytes].decode("utf-8", errors="ignore") + "\n<!-- truncated -->"
    return text


def _bare(raw: str) -> str:
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
        return raw[1:-1]
    return raw


def mask_text(text: str, *, known_secrets: Iterable[str] = (), limit: int = 2000) -> str:
    """Console/log line sanitization: declared secret values plus recognised secret shapes."""
    return redact(text, tuple(item for item in known_secrets if item))[:limit]
