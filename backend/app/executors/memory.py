"""Element memory storage behind the locator's candidate cache (§8.3).

Every read is advisory: the locator re-validates a remembered selector against the live page before
it may act on it, so a stale row can slow a step down but cannot silently mis-target it.
"""

from __future__ import annotations

from ..db.base import get_database
from ..observability import get_logger

log = get_logger(__name__)


class DatabaseElementMemory:
    """Scoped to one execution's environment, origin and browser family."""

    def __init__(
        self,
        *,
        tenant_id: str,
        project_id: str,
        environment_id: str,
        browser_family: str = "chromium",
        limit: int = 3,
    ) -> None:
        self.tenant_id = tenant_id
        self.project_id = project_id
        self.environment_id = environment_id
        self.browser_family = browser_family
        self.limit = limit

    def candidates(self, *, origin: str, route_pattern: str, target_fingerprint: str) -> list[tuple[str, str]]:
        from ..repositories.artifacts import ElementMemoryRepository

        with get_database().session(self.tenant_id) as session:
            rows = ElementMemoryRepository(session, self.tenant_id).candidates(
                environment_id=self.environment_id,
                origin=origin,
                route_pattern=route_pattern,
                target_fingerprint=target_fingerprint,
                browser_family=self.browser_family,
                limit=self.limit,
            )
            return [(row.strategy, row.selector) for row in rows]

    def note_success(
        self,
        *,
        origin: str,
        route_pattern: str,
        target_fingerprint: str,
        strategy: str,
        selector: str,
        description: str | None = None,
        app_version: str | None = None,
    ) -> None:
        from ..repositories.artifacts import ElementMemoryRepository

        with get_database().session(self.tenant_id) as session:
            ElementMemoryRepository(session, self.tenant_id).note_success(
                project_id=self.project_id,
                environment_id=self.environment_id,
                origin=origin,
                route_pattern=route_pattern,
                browser_family=self.browser_family,
                target_fingerprint=target_fingerprint,
                description=description,
                strategy=strategy,
                selector=selector,
                app_version=app_version,
            )
            session.commit()

    def note_failure(
        self,
        *,
        origin: str,
        route_pattern: str,
        target_fingerprint: str,
        strategy: str,
        selector: str,
    ) -> None:
        from ..repositories.artifacts import ElementMemoryRepository

        with get_database().session(self.tenant_id) as session:
            ElementMemoryRepository(session, self.tenant_id).note_failure(
                environment_id=self.environment_id,
                origin=origin,
                route_pattern=route_pattern,
                strategy=strategy,
                selector=selector,
                target_fingerprint=target_fingerprint,
                browser_family=self.browser_family,
            )
            session.commit()


class NoElementMemory:
    """Used when the environment disables candidate reuse or the tenant has no approved rows."""

    def candidates(self, **_: object) -> list[tuple[str, str]]:
        return []

    def note_success(self, **_: object) -> None:
        return None

    def note_failure(self, **_: object) -> None:
        return None
