"""Runtime configuration (detailed design §15.2) with startup cross-validation."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_TRUE = {"1", "true", "yes", "on"}

#: Shipped so a laptop run needs no secret; `validate_runtime` refuses them anywhere but development.
DEFAULT_DEV_TOKENS = frozenset({"dev-admin-token", "dev-engineer-token"})

AppEnv = Literal["development", "test", "production"]


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.environ.get("AITA_ENV_FILE", str(_project_root() / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- application ---
    app_env: AppEnv = "development"
    #: Loopback by default: a container or a reverse proxy sets API_HOST=0.0.0.0 deliberately.
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    web_origin: str = "http://localhost:5173"
    log_level: str = "INFO"
    data_dir: Path = Field(default_factory=lambda: _project_root() / "data")

    # --- data dependencies ---
    database_url: str = "sqlite:///./data/ai_test_agent.db"
    db_pool_size: int = 5
    redis_url: str = "redis://127.0.0.1:6379/0"
    queue_backend: Literal["auto", "celery", "inprocess"] = "auto"
    celery_broker_transport_options_timeout: int = 1800

    object_store: Literal["local", "s3"] = "local"
    object_store_endpoint: str | None = None
    object_store_bucket: str = "ai-test-agent"
    object_store_region: str = "us-east-1"
    object_store_access_key: str | None = None
    object_store_secret_key: str | None = None

    secret_provider: Literal["local_fernet", "kms"] = "local_fernet"  # noqa: S105  # a provider name, not a secret
    secret_master_key: str | None = None

    # --- identity and RBAC ---
    auth_mode: Literal["dev", "oidc"] = "dev"
    dev_admin_token: str = "dev-admin-token"  # noqa: S105  # refused by validate_runtime outside development
    dev_engineer_token: str = "dev-engineer-token"  # noqa: S105  # refused by validate_runtime outside development
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_uri: str | None = None

    # --- execution budgets (§9.5) ---
    worker_slots: int = 4
    worker_pool_name: str = "default"
    queue_timeout_seconds: int = 600
    step_timeout_ms: int = 10_000
    navigation_timeout_ms: int = 30_000
    step_timeout_max_ms: int = 120_000
    vision_step_budget_ms: int = 20_000
    vision_deterministic_ms: int = 5_000
    vision_call_budget_ms: int = 10_000
    vision_reserve_action_ms: int = 5_000
    wait_duration_max_ms: int = 10_000
    active_timeout_seconds: int = 600
    human_wait_timeout_seconds: int = 300
    max_human_tasks_per_run: int = 2
    max_human_wait_seconds: int = 600
    human_slot_ratio: float = 0.25
    finalization_timeout_seconds: int = 60
    task_hard_limit_seconds: int = 1500
    visibility_timeout_seconds: int = 1800

    # --- leases ---
    lease_heartbeat_seconds: float = 5.0
    lease_ttl_seconds: int = 30
    worker_drain_timeout_seconds: int = 300
    reconciler_interval_seconds: float = 5.0
    outbox_poll_interval_seconds: float = 0.5
    scheduler_poll_interval_seconds: float = 1.0
    reservation_ttl_seconds: int = 60

    # --- browser ---
    browser_channels: str = "chromium,chrome"
    browser_headless: bool = True
    browser_sandbox: bool = True
    viewport_width: int = 1280
    viewport_height: int = 720
    browser_locale: str = "zh-CN"
    browser_timezone: str = "Asia/Shanghai"
    browser_executable_path: str | None = None
    playwright_browsers_path: str | None = None
    ignore_https_errors: bool = False

    # --- AI ---
    ai_enabled: bool = True
    ai_base_url: str = "https://api.openai.com/v1"
    ai_model: str = "gpt-4o-mini"
    ai_api_key: str | None = None
    ai_timeout_seconds: float = 30.0
    ai_max_calls_per_run: int = 2
    ai_max_total_ms: int = 60_000
    ai_compiler_max_calls: int = Field(default=400, ge=1)
    ai_compiler_max_total_ms: int = Field(default=600_000, ge=1)
    ai_analysis_images_enabled: bool = False
    ai_max_output_tokens: int = 2048
    ai_prompt_version: str = "compiler-1.0/analysis-1.0"
    ai_vision_enabled: bool = False
    ai_budget_tokens_per_tenant: int = 2_000_000
    ai_cache_dir: Path | None = None

    # --- evidence (§12.1) ---
    screenshot_policy: Literal["on_failure", "always", "off"] = "on_failure"
    trace_policy: Literal["off", "retain_on_failure"] = "off"
    video_policy: Literal["off", "on"] = "off"
    artifact_max_bytes: int = 200 * 1024 * 1024
    dom_evidence_max_bytes: int = 2 * 1024 * 1024
    console_ring_max: int = 2000
    network_ring_max: int = 2000
    evidence_disk_budget_bytes: int = 200 * 1024 * 1024
    retention_normal_days: int = 30
    retention_failure_days: int = 90
    retention_audit_days: int = 180
    artifact_access_ttl_seconds: int = 60

    # --- human control ---
    human_control_ticket_ttl_seconds: int = 60
    human_frame_interval_ms: int = 500

    # --- limits ---
    max_case_bytes: int = 262_144
    max_case_steps: int = 200
    max_field_bytes: int = 16_384
    max_attachment_bytes: int = 20 * 1024 * 1024
    max_request_body_bytes: int = 32 * 1024 * 1024
    idempotency_ttl_hours: int = 24

    # --- egress (§14.2) ---
    egress_block_metadata_ips: bool = True
    egress_allow_loopback: bool = True
    egress_allow_private: bool = True
    egress_allow_http: bool = True

    @field_validator("data_dir", "ai_cache_dir", mode="after")
    @classmethod
    def _absolute(cls, value: Path | None) -> Path | None:
        return None if value is None else (value if value.is_absolute() else _project_root() / value)

    @property
    def is_development(self) -> bool:
        return self.app_env in ("development", "test")

    @property
    def browser_channel_list(self) -> list[str]:
        return [item.strip() for item in self.browser_channels.split(",") if item.strip()]

    @property
    def artifact_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def scratch_dir(self) -> Path:
        return self.data_dir / "scratch"

    def validate_runtime(self) -> None:
        """Cross-field validation required by §15.2; raises ValueError on misconfiguration."""
        problems: list[str] = []
        if self.visibility_timeout_seconds <= self.task_hard_limit_seconds:
            problems.append("visibility_timeout_seconds must exceed task_hard_limit_seconds")
        if self.lease_ttl_seconds <= self.lease_heartbeat_seconds * 3:
            problems.append("lease_ttl_seconds must exceed lease_heartbeat_seconds x 3")
        if self.task_hard_limit_seconds < self.active_timeout_seconds + self.human_wait_timeout_seconds:
            problems.append("task_hard_limit_seconds must cover active + human wait budgets")
        if self.step_timeout_ms > self.step_timeout_max_ms:
            problems.append("step_timeout_ms must not exceed step_timeout_max_ms")
        if self.queue_backend == "celery" and not self.redis_url:
            problems.append("redis_url is required when queue_backend=celery")
        if self.ai_enabled and not self.ai_base_url:
            problems.append("ai_base_url is required when ai_enabled")
        if self.secret_provider != "local_fernet":  # noqa: S105 (provider name, not a credential)
            problems.append(
                "only local_fernet is implemented; inject its master key through the deployment secret manager"
            )
        if not self.is_development:
            problems.extend(self._production_problems())
        if problems:
            raise ValueError("; ".join(problems))

    def _production_problems(self) -> list[str]:
        """A production process must not start with the development credential path open (§14.1)."""
        problems: list[str] = []
        if self.auth_mode != "oidc":
            problems.append("auth_mode must be oidc outside development")
        if self.dev_admin_token in DEFAULT_DEV_TOKENS or self.dev_engineer_token in DEFAULT_DEV_TOKENS:
            problems.append("dev_admin_token/dev_engineer_token must be replaced outside development")
        if self.auth_mode == "oidc" and not (self.oidc_issuer and self.oidc_jwks_uri):
            problems.append("oidc_issuer and oidc_jwks_uri are required when auth_mode=oidc")
        # "local_fernet" names a provider, it is not a credential.
        if not self.secret_master_key:
            problems.append("secret_master_key is required for secrets and stored test-data encryption")
        if self.database_url.startswith("sqlite"):
            problems.append("a sqlite database is not supported outside development")
        return problems

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.artifact_dir, self.scratch_dir, self.local_store_dir):
            path.mkdir(parents=True, exist_ok=True)

    @property
    def local_store_dir(self) -> Path:
        return self.data_dir / "object-store"

    def resolved_database_url(self) -> str:
        """Make a relative sqlite path absolute against the project root, so any cwd works.

        The default `sqlite:///./data/app.db` is written as though the process started from the
        repository root; resolving it here keeps `uvicorn`, the worker and a test run pointed at one
        file instead of one database per working directory. An absolute path and any other backend
        URL are passed through untouched.
        """
        prefix = "sqlite:///"
        if self.database_url.startswith(prefix):
            rel = self.database_url[len(prefix) :]
            if rel and not rel.startswith("/"):
                path = (_project_root() / rel).resolve()
                path.parent.mkdir(parents=True, exist_ok=True)
                return prefix + str(path)
        return self.database_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.validate_runtime()
    if settings.playwright_browsers_path:
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(settings.playwright_browsers_path))
    return settings
