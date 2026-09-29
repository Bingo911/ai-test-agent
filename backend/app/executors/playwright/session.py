"""Browser session lifecycle for one execution (§2.2, §14.2).

One execution owns one Playwright instance, one browser, one context and exactly one page. Objects
are never shared across forked workers or threads, and the session is destroyed when the execution
ends so cookies and storage cannot leak into another tenant's run.
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from ...evidence.collector import EvidenceCollector
from ...observability import get_logger
from ..contracts import SessionConfig

log = get_logger(__name__)

FORBIDDEN_SCHEMES = ("file", "javascript", "data", "view-source")
CLOUD_METADATA_HOSTS = (
    "169.254.169.254",
    "metadata.google.internal",
    "metadata.internal",
    "100.100.100.200",  # Alibaba Cloud
)
DOCUMENT_RESOURCE_TYPES = ("document", "other")


class EgressViolation(Exception):
    """A navigation or sub-resource the environment policy does not allow (§14.2)."""

    def __init__(self, url: str, reason: str) -> None:
        self.url = url
        self.reason = reason
        super().__init__(f"{reason}: {url}")


class UnsupportedTargetScope(Exception):
    """iframe or multi-tab work V1 does not do (§2.2): report instead of guessing."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def host_allowed(host: str, allowed_domains: tuple[str, ...]) -> bool:
    """Suffix match against the environment's allowlist; `sub.example.com` needs `example.com` or exact."""
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    for candidate in allowed_domains:
        entry = candidate.strip().lower().lstrip(".")
        if not entry:
            continue
        if entry == host:
            return True
        if host.endswith("." + entry):
            return True
        if address is not None and entry == address.compressed:
            return True
    return False


def check_url_shape(url: str, *, allow_http: bool, allow_loopback: bool, allow_private: bool) -> str | None:
    """Scheme and destination screening applied to every request, including redirects (§14.2)."""
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme in FORBIDDEN_SCHEMES:
        return f"scheme '{scheme}' is never allowed"
    if scheme not in {"http", "https"}:
        return f"scheme '{scheme}' is not supported"
    if scheme == "http" and not allow_http:
        return "plain http is disabled by policy"
    host = (parts.hostname or "").lower()
    if not host:
        return "the URL has no host"
    if host in CLOUD_METADATA_HOSTS or host.endswith(".metadata"):
        return "cloud metadata endpoints are blocked"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if address.is_loopback and not allow_loopback:
        return "loopback destinations are blocked"
    if address.is_link_local:
        return "link-local destinations are blocked"
    if address.is_private and not allow_private:
        return "private destinations need an explicit approval"
    return None


@dataclass
class BrowserSession:
    """Handles plus the identity of the browser that actually ran the steps (§7.2)."""

    playwright: Any
    browser: Any
    context: Any
    page: Any
    collector: EvidenceCollector
    config: SessionConfig
    browser_version: str
    browser_family: str
    started_monotonic_ms: float
    scratch_dir: Any
    trace_started: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)
    scope_violations: list[str] = field(default_factory=list)

    @property
    def elapsed_ms(self) -> int:
        return max(0, int(time.monotonic() * 1000 - self.started_monotonic_ms))


class PlaywrightSessionFactory:
    """Launches the browser and wires the egress route, listeners and evidence capture."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    async def create(self, config: SessionConfig, collector: EvidenceCollector, *, scratch_dir: Any) -> BrowserSession:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError("playwright is not installed; run `playwright install chromium`") from exc

        if config.browsers_path:
            os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(config.browsers_path))

        launch_options: dict[str, Any] = {
            "headless": config.headless,
            "args": ["--no-sandbox"] if not config.sandbox else [],
        }
        if config.browser_type == "chromium" and config.browser_channel:
            launch_options["channel"] = config.browser_channel
        if config.executable_path:
            launch_options["executable_path"] = config.executable_path

        playwright = await async_playwright().start()
        browser = None
        try:
            launcher = getattr(playwright, config.browser_type, None) or playwright.chromium
            browser = await launcher.launch(**launch_options)
        except Exception as exc:
            await playwright.stop()
            raise BrowserStartFailure(str(exc)) from exc

        version = browser.version
        context_options: dict[str, Any] = {
            "viewport": {"width": config.viewport[0], "height": config.viewport[1]},
            "locale": config.locale,
            "timezone_id": config.timezone,
            "device_scale_factor": config.device_scale_factor,
            "ignore_https_errors": config.ignore_https_errors,
            "java_script_enabled": True,
        }
        if config.user_agent:
            context_options["user_agent"] = config.user_agent
        if config.record_video:
            video_dir = scratch_dir / "video"
            video_dir.mkdir(parents=True, exist_ok=True)
            context_options["record_video_dir"] = str(video_dir)
            context_options["record_video_size"] = {"width": config.viewport[0], "height": config.viewport[1]}

        context = await browser.new_context(**context_options)
        context.set_default_timeout(config.navigation_timeout_ms)
        context.set_default_navigation_timeout(config.navigation_timeout_ms)
        collector.viewport = config.viewport

        session = BrowserSession(
            playwright=playwright,
            browser=browser,
            context=context,
            page=None,
            collector=collector,
            config=config,
            browser_version=version,
            browser_family=config.browser_type,
            started_monotonic_ms=time.monotonic() * 1000,
            scratch_dir=scratch_dir,
        )

        await self._install_egress_guard(context, config)
        context.on("page", lambda page: self._on_unexpected_page(session, page))
        collector.wire_context(context)

        page = await context.new_page()
        session.page = page
        collector.wire_page(page)
        if config.start_trace:
            await context.tracing.start(screenshots=True, snapshots=True, sources=True)
            session.trace_started = True
        return session

    async def _install_egress_guard(self, context: Any, config: SessionConfig) -> None:
        """Supplementary in-browser enforcement (§14.2): the proxy remains the primary control."""
        settings = self.settings
        allowed = tuple(config.allowed_origins)

        async def guard(route: Any) -> None:
            request = route.request
            url = request.url
            reason = check_url_shape(
                url,
                allow_http=bool(getattr(settings, "egress_allow_http", True)),
                allow_loopback=bool(getattr(settings, "egress_allow_loopback", True)),
                allow_private=bool(getattr(settings, "egress_allow_private", True)),
            )
            if reason is None and str(getattr(request, "resource_type", "")) in DOCUMENT_RESOURCE_TYPES and allowed:
                host = (urlsplit(url).hostname or "").lower()
                if not host_allowed(host, allowed):
                    reason = "the navigation target is outside the environment allowlist"
            if reason is not None:
                log.warning("egress blocked", extra={"context": {"url": url, "reason": reason}})
                await route.abort("blockedbyclient")
                return
            await route.continue_()

        await context.route("**/*", guard)

    def _on_unexpected_page(self, session: BrowserSession, page: Any) -> None:
        """A second tab means the target opened something V1 cannot orchestrate (§2.2)."""
        if session.page is None or page is session.page:
            return
        session.scope_violations.append(f"unexpected page: {getattr(page, 'url', '')}")
        import asyncio

        async def close() -> None:
            with contextlib.suppress(Exception):  # pragma: no cover - already closing
                await page.close()

        # pragma: no cover - no loop means the session is already gone
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(close())


class BrowserStartFailure(Exception):
    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"browser could not be started: {detail}")
