"""Composition root for the API process (§13.2).

In production this serves HTTP only and Celery workers run the control loops. In development the
same process also owns those loops, so `uvicorn app.main:app` is a complete, runnable platform.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import cases, environments, evidence, executions, humans, projects, quality, system
from .api.deps import RequestContextMiddleware, app_database, register_exception_handlers
from .config import Settings, get_settings
from .db.base import Database
from .db.bootstrap import bootstrap_runtime
from .observability import configure_logging, get_logger
from .orchestrator.queue import InProcessQueue

log = get_logger(__name__)

API_PREFIX = "/api/v1"

#: Keeps the two same-named legacy routes from reappearing: the parser prototype was replaced by the
#: compile pipeline, and a stale path in the docs invites clients to build against a dead contract.
OPENAPI_TAGS = [
    {"name": "system", "description": "Health, capability discovery and the caller's own authority."},
    {"name": "projects", "description": "Projects, membership and permission grants (§14.1)."},
    {"name": "cases", "description": "Case drafts, revisions, compilation and confirmation (§9.2)."},
    {"name": "environments", "description": "Environment records, revisions and secrets (§10.1)."},
    {"name": "executions", "description": "Run creation, state, events, reports and analysis (§9.1)."},
    {"name": "human", "description": "Human-assist tasks, control tickets and live frames (§10.3)."},
    {"name": "evidence", "description": "Download tickets and authorized artifact proxying (§12.1)."},
    {"name": "quality", "description": "Aggregate metrics and the audit trail (§16.1)."},
]

#: A reload must not leave a second set of loops publishing to the same queue.
SHUTDOWN_DRAIN_SECONDS = 10.0


def _owns_loops(settings: Settings) -> bool:
    """Only the single-process development server runs the loops in-process (§15.1)."""
    return settings.app_env == "development"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = app.state.settings
    settings.ensure_dirs()
    settings.validate_runtime()
    workspace = bootstrap_runtime(settings, database=app_database(app), seed=settings.is_development)
    bundle = getattr(app.state, "mcp", None)
    if bundle is not None:
        # The manager must be entered before the first MCP request can be answered, and it is entered
        # here rather than by the mounted sub-application because a mounted Starlette app never runs
        # its own lifespan (§4.3).
        await bundle.start()
    supervisor = None
    if _owns_loops(settings):
        from .orchestrator.runtime import Supervisor

        supervisor = Supervisor(settings).start()
    log.info(
        "api started",
        extra={
            "context": {
                "env": settings.app_env,
                "auth_mode": settings.auth_mode,
                "queue_backend": settings.queue_backend,
                "loops_in_process": supervisor is not None,
                "mcp_enabled": bundle is not None,
                "project_id": (workspace or {}).get("project_id"),
            }
        },
    )
    try:
        yield
    finally:
        if bundle is not None:
            # MCP unwinds first: new commands are refused, then this adapter's threads and sockets
            # close, and only then does the SDK session manager leave (§4.3).
            await bundle.stop()
        if supervisor is not None:
            supervisor.stop()
            queue = supervisor.queue
            # Tasks already published are given a moment to finish; anything left is handed to the
            # reconciler, which treats an abandoned lease as a worker loss rather than a lost run.
            if isinstance(queue, InProcessQueue):
                queue.drain(timeout=SHUTDOWN_DRAIN_SECONDS)
                queue.close()
        log.info("api stopped", extra={"context": {"env": settings.app_env}})


def create_app(
    settings: Settings | None = None,
    *,
    database: Database | None = None,
    openapi_extra: dict[str, Any] | None = None,
) -> FastAPI:
    """Build one app with its own Settings and Database; nothing below re-reads the environment (§4.4).

    `database` defaults to the process-wide one, which is what a single-root deployment has always used.
    A factory given a second database gets a second, independent app: the two never share a pool, and
    neither reaches the other's through a module global.
    """
    settings = settings or get_settings()
    configure_logging(settings.log_level, json_output=not settings.is_development)

    application = FastAPI(
        title="AI Test Agent",
        version="1.0.0",
        summary="Markdown test cases to deterministic, evidence-backed browser runs.",
        description=(
            "Cases are compiled into a validated Test IR, executed on the caller's own browser under a "
            "resourced state machine, and reported with evidence. Requests are tenant-scoped; writes may "
            "carry an Idempotency-Key to replay their own first response, and versioned resources accept "
            "If-Match."
        ),
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=None,
        openapi_url=f"{API_PREFIX}/openapi.json",
        lifespan=lifespan,
        openapi_tags=OPENAPI_TAGS,
    )
    application.state.settings = settings
    application.state.database = database

    # Outermost first: an error response still needs CORS headers, or the console only sees a network fault.
    application.add_middleware(RequestContextMiddleware, max_body_bytes=settings.max_request_body_bytes)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.web_origin],
        # Tokens travel in the Authorization header, so no cookie or client certificate is ever sent.
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "If-Match",
            "Idempotency-Key",
            "X-Request-ID",
            "X-Tenant-Id",
        ],
        expose_headers=["ETag", "X-Request-ID"],
        max_age=600,
    )
    register_exception_handlers(application)

    for module in (system, projects, cases, environments, executions, humans, evidence, quality):
        application.include_router(module.router, prefix=API_PREFIX)
    if settings.mcp_enabled:
        # Imported only when the switch is on, so a disabled deployment never loads the SDK, builds a
        # session manager or opens the MCP pool (§12.2 item 1).
        from .mcp.readiness import add_readiness_route
        from .mcp.transport import McpDispatchMiddleware, build_mcp_bundle

        bundle = build_mcp_bundle(settings)
        application.state.mcp = bundle
        add_readiness_route(application, settings)
        # Added last so it wraps the REST CORS stack: an MCP request never meets the console's narrower
        # header policy, and anything that is not an exact MCP path reaches the original router (§4.1).
        application.add_middleware(McpDispatchMiddleware, bundle=bundle)
    if openapi_extra:
        application.openapi_schema = {**(application.openapi_schema or {}), **openapi_extra}
    return application


app = create_app()


def main() -> None:
    """Serve the API on the configured bind, so `API_HOST`/`API_PORT` mean what the operator set (§15.1)."""
    import uvicorn

    settings = get_settings()
    # `log_config=None` keeps uvicorn from reinstalling the root logger that configure_logging owns.
    # The forwarding trust list is passed explicitly rather than left to uvicorn's own env fallback, so the
    # address this process believes is the one its configuration says (§13.3).
    uvicorn.run(
        app,
        host=settings.api_host,
        port=settings.api_port,
        proxy_headers=settings.proxy_headers,
        forwarded_allow_ips=settings.resolved_forwarded_allow_ips,
        log_config=None,
    )


if __name__ == "__main__":
    main()
