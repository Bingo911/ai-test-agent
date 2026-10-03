"""Publishing environment revisions (§11.2, §14.2, §14.3).

A published revision is the network and evidence policy for every run that freezes it, so the shape is
checked here rather than at execution time: a typo in `allowed_domains` must not turn into a browser
that reaches an unapproved host.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any
from urllib.parse import urlsplit

from ..config import Settings, get_settings
from ..db.base import Database, get_database
from ..domain.enums import Sensitivity
from ..domain.errors import ApiError, ErrorCode
from ..repositories.platform import AccessRepository
from ..repositories.resources import EnvironmentRepository
from ..services.secret_store import SecretStore, get_secret_store

#: §14.2: these destinations are never allowed, whatever a project's own policy says.
FORBIDDEN_HOSTS = frozenset({"169.254.169.254", "metadata.google.internal", "metadata", "fd00:ec2::254"})
_HOSTNAME = re.compile(r"^(?!-)[A-Za-z0-9-._]{1,253}(?<!-)$")
_VARIABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,59}$")
CONFIG_KEYS = frozenset(
    {"base_url", "allowed_domains", "allowed_protocols", "browsers", "viewport", "evidence", "variables"}
)


class EnvironmentService:
    def __init__(self, settings: Settings | None = None, *, database: Database | None = None) -> None:
        self.settings = settings or get_settings()
        self._database = database

    @property
    def database(self) -> Database:
        """The pool this service was handed; the process default only serves standalone callers."""
        return self._database if self._database is not None else get_database()

    def create(self, *, tenant_id: str, project_id: str, name: str, created_by: str | None) -> dict[str, Any]:
        with self.database.session(tenant_id) as session:
            repos = EnvironmentRepository(session, tenant_id)
            if repos.by_name(project_id, name) is not None:
                raise ApiError(ErrorCode.CONFLICT, f"Environment '{name}' already exists in this project")
            environment = repos.create(project_id=project_id, name=name, created_by=created_by)
            session.commit()
            return _environment_payload(environment, revision=None)

    def publish(
        self,
        *,
        tenant_id: str,
        environment_id: str,
        config: dict[str, Any],
        secret_bindings: dict[str, Any] | None,
        created_by: str | None,
        expected_row_version: int | None = None,
    ) -> dict[str, Any]:
        with self.database.session(tenant_id) as session:
            repos = EnvironmentRepository(session, tenant_id)
            environment = repos.by_id(environment_id)
            if environment is None:
                raise ApiError(ErrorCode.NOT_FOUND, "Environment not found in this tenant")
            if environment.archived_at is not None:
                raise ApiError(ErrorCode.CONFLICT, "An archived environment cannot take new revisions")
            cleaned = validate_config(config, settings=self.settings)
            bindings = self._validate_bindings(
                session,
                tenant_id=tenant_id,
                project_id=environment.project_id,
                config=cleaned,
                bindings=dict(secret_bindings or {}),
            )
            revision = repos.publish_revision(
                environment,
                config=cleaned,
                secret_bindings=bindings,
                created_by=created_by,
                expected_row_version=expected_row_version,
            )
            session.commit()
            return _environment_payload(environment, revision=revision)

    def _validate_bindings(
        self,
        session,
        *,
        tenant_id: str,
        project_id: str,
        config: dict[str, Any],
        bindings: dict[str, Any],
    ) -> dict[str, Any]:
        """A binding that points at a missing or revoked secret must fail here, not at run time (§14.3)."""
        store = get_secret_store(self.settings)
        ir_secrets = set(_config_secret_keys(config))
        cleaned: dict[str, Any] = {}
        for name, version in bindings.items():
            record = store.get(
                session, tenant_id=tenant_id, project_id=project_id, logical_name=name, version=_version(version)
            )
            if record is None:
                raise ApiError(
                    ErrorCode.SECRET_UNAVAILABLE,
                    f"Secret '{name}' is not defined in this project at that version",
                    details={"logical_name": name, "version": version},
                )
            if record.status != "ACTIVE":
                raise ApiError(
                    ErrorCode.SECRET_VERSION_REVOKED,
                    f"Secret '{name}' v{record.version} is revoked; publish a new version to keep using it",
                    details={"logical_name": name, "version": int(record.version)},
                )
            cleaned[name] = int(record.version)
        missing = sorted(ir_secrets - set(cleaned))
        if missing:
            raise ApiError(
                ErrorCode.SECRET_UNAVAILABLE,
                f"The configuration references secrets with no binding: {', '.join(missing)}",
                details={"unbound": missing},
            )
        return cleaned

    def archive(self, *, tenant_id: str, environment_id: str) -> dict[str, Any]:
        with self.database.session(tenant_id) as session:
            repos = EnvironmentRepository(session, tenant_id)
            environment = repos.by_id(environment_id)
            if environment is None:
                raise ApiError(ErrorCode.NOT_FOUND, "Environment not found in this tenant")
            repos.archive(environment)
            session.commit()
            return _environment_payload(environment, revision=None)

    def list(self, *, tenant_id: str, project_id: str) -> list[dict[str, Any]]:
        with self.database.session(tenant_id) as session:
            repos = EnvironmentRepository(session, tenant_id)
            out = []
            for environment in repos.list(project_id):
                out.append(_environment_payload(environment, revision=repos.current_revision(environment)))
            return out

    def by_name(self, *, tenant_id: str, project_id: str, name: str) -> dict[str, Any]:
        with self.database.session(tenant_id) as session:
            repos = EnvironmentRepository(session, tenant_id)
            environment = repos.by_name(project_id, name)
            if environment is None:
                raise ApiError(ErrorCode.NOT_FOUND, "Environment not found in this project")
            return _environment_payload(environment, revision=repos.current_revision(environment))

    def revisions(self, *, tenant_id: str, environment_id: str) -> list[dict[str, Any]]:
        with self.database.session(tenant_id) as session:
            repos = EnvironmentRepository(session, tenant_id)
            environment = repos.by_id(environment_id)
            if environment is None:
                raise ApiError(ErrorCode.NOT_FOUND, "Environment not found in this tenant")
            return [
                _revision_payload(row, current=environment.current_revision_id)
                for row in repos.revisions(environment_id)
            ]

    def secret_versions(self, *, tenant_id: str, project_id: str) -> list[dict[str, Any]]:
        with self.database.session(tenant_id) as session:
            AccessRepository(session, tenant_id).require(project_id)
            return get_secret_store(self.settings).list_for_project(session, tenant_id=tenant_id, project_id=project_id)

    def put_secret(
        self,
        *,
        tenant_id: str,
        project_id: str,
        logical_name: str,
        value: str,
        created_by: str | None,
    ) -> dict[str, Any]:
        """Write-only: the response carries the version reference, never the value (§13.2)."""
        cleaned = logical_name.strip()
        if not _VARIABLE.match(cleaned):
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "Secret names must be identifiers, because they appear in ${secrets.<name>} references",
                details={"logical_name": logical_name[:60]},
            )
        with self.database.session(tenant_id) as session:
            AccessRepository(session, tenant_id).require(project_id)
            store = get_secret_store(self.settings)
            record = store.put(
                session,
                tenant_id=tenant_id,
                project_id=project_id,
                logical_name=cleaned,
                value=value,
                created_by=created_by,
            )
            session.commit()
            return {
                "secret_version_id": record.id,
                "logical_name": record.logical_name,
                "version": int(record.version),
                "provider": record.provider,
                "status": record.status,
                "reference": f"${{secrets.{record.logical_name}}}",
            }

    def revoke_secret(self, *, tenant_id: str, project_id: str, logical_name: str, version: int) -> dict[str, Any]:
        with self.database.session(tenant_id) as session:
            AccessRepository(session, tenant_id).require(project_id)
            store: SecretStore = get_secret_store(self.settings)
            store.revoke(
                session, tenant_id=tenant_id, project_id=project_id, logical_name=logical_name, version=version
            )
            session.commit()
            return {"logical_name": logical_name, "version": version, "status": "REVOKED"}


# ------------------------------------------------------------------------ validation


def validate_config(config: dict[str, Any], *, settings: Settings | None = None) -> dict[str, Any]:
    """Normalise and reject: returns a fresh dict safe to freeze into a revision."""
    settings = settings or get_settings()
    if not isinstance(config, dict):
        raise ApiError(ErrorCode.VALIDATION_ERROR, "Environment config must be an object")
    unknown = sorted(set(config) - CONFIG_KEYS)
    if unknown:
        raise ApiError(
            ErrorCode.SEMANTIC_ERROR,
            f"Unknown environment config keys: {', '.join(unknown)}",
            details={"unknown": unknown, "allowed": sorted(CONFIG_KEYS)},
        )
    cleaned: dict[str, Any] = {}

    base_url = str(config.get("base_url") or "").strip()
    cleaned["base_url"] = _check_base_url(base_url)

    domains = _string_list(config.get("allowed_domains"), "allowed_domains")
    if not domains:
        raise ApiError(ErrorCode.SEMANTIC_ERROR, "allowed_domains must list at least one host for the run to reach")
    cleaned["allowed_domains"] = sorted({_check_host(item) for item in domains})

    protocols = _string_list(config.get("allowed_protocols") or ["https"], "allowed_protocols")
    for protocol in protocols:
        if protocol not in ("https", "http"):
            raise ApiError(
                ErrorCode.SEMANTIC_ERROR,
                f"Protocol '{protocol}' is never allowed; navigation is limited to http(s)",
                details={"protocol": protocol},
            )
    cleaned["allowed_protocols"] = sorted(set(protocols))

    browsers = _string_list(config.get("browsers"), "browsers") or settings.browser_channel_list
    supported = set(settings.browser_channel_list)
    rejected = sorted(set(browsers) - supported)
    if rejected:
        raise ApiError(
            ErrorCode.BROWSER_NOT_SUPPORTED,
            f"This deployment has no browser channel for: {', '.join(rejected)}",
            details={"unsupported": rejected, "supported": sorted(supported)},
        )
    cleaned["browsers"] = sorted(set(browsers))

    cleaned["viewport"] = _viewport(config.get("viewport"), settings)
    cleaned["evidence"] = _evidence(config.get("evidence"))
    cleaned["variables"] = _variables(config.get("variables"))
    return cleaned


def _check_base_url(value: str) -> str:
    if not value:
        raise ApiError(ErrorCode.SEMANTIC_ERROR, "base_url is required so the run knows where to start")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https"):
        raise ApiError(
            ErrorCode.DOMAIN_NOT_ALLOWED,
            "base_url must be an http(s) URL",
            details={"scheme": parts.scheme or None},
        )
    if not parts.hostname:
        raise ApiError(ErrorCode.SEMANTIC_ERROR, "base_url has no host to navigate to")
    if parts.username or parts.password:
        raise ApiError(
            ErrorCode.SECRET_FIELD_NOT_ALLOWED,
            "Credentials in base_url are rejected; bind a secret instead of putting one in a URL",
        )
    return value.rstrip("/")


def _check_host(value: str) -> str:
    host = value.strip().lower().lstrip(".").rstrip("/")
    if not host or "://" in host or "/" in host or " " in host:
        raise ApiError(
            ErrorCode.SEMANTIC_ERROR,
            f"'{value}' is not a hostname; allowed_domains takes hosts, not URLs",
            details={"entry": value[:120]},
        )
    if host in FORBIDDEN_HOSTS:
        raise ApiError(
            ErrorCode.DOMAIN_NOT_ALLOWED, f"'{host}' is a cloud metadata endpoint and may never be allowlisted"
        )
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not _HOSTNAME.match(host):
            raise ApiError(
                ErrorCode.SEMANTIC_ERROR, f"'{value}' is not a valid hostname", details={"entry": host[:120]}
            ) from None
        return host
    if address.is_multicast or address.is_unspecified or host in FORBIDDEN_HOSTS:
        raise ApiError(ErrorCode.DOMAIN_NOT_ALLOWED, f"Address '{host}' may not be allowlisted")
    return host


def _viewport(value: Any, settings: Settings) -> dict[str, int]:
    raw = value if isinstance(value, dict) else {}
    width = int(raw.get("width") or settings.viewport_width)
    height = int(raw.get("height") or settings.viewport_height)
    if not (320 <= width <= 3840 and 240 <= height <= 2160):
        raise ApiError(
            ErrorCode.SEMANTIC_ERROR,
            "viewport must be between 320x240 and 3840x2160",
            details={"viewport": {"width": width, "height": height}},
        )
    return {"width": width, "height": height}


def _evidence(value: Any) -> dict[str, str]:
    raw = value if isinstance(value, dict) else {}
    mode = str(raw.get("mode") or Sensitivity.NORMAL.value).upper()
    trace = str(raw.get("trace") or "off").lower()
    video = str(raw.get("video") or "off").lower()
    if mode not in (Sensitivity.NORMAL.value, Sensitivity.SENSITIVE.value):
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, f"Unknown evidence mode '{mode}'", details={"allowed": ["NORMAL", "SENSITIVE"]}
        )
    if trace not in ("off", "on", "retain_on_failure"):
        raise ApiError(ErrorCode.VALIDATION_ERROR, f"Unknown trace policy '{trace}'")
    if video not in ("off", "on"):
        raise ApiError(ErrorCode.VALIDATION_ERROR, f"Unknown video policy '{video}'")
    return {"mode": mode, "trace": trace, "video": video}


def _variables(value: Any) -> dict[str, Any]:
    raw = value or {}
    if not isinstance(raw, dict):
        raise ApiError(ErrorCode.VARIABLES_INVALID, "Environment variables must be an object of plain values")
    cleaned: dict[str, Any] = {}
    for key, item in raw.items():
        if not _VARIABLE.match(str(key)):
            raise ApiError(ErrorCode.VARIABLE_DEFINITION_INVALID, f"Variable name '{key}' is not an identifier")
        if isinstance(item, (str, int, float, bool)) or item is None:
            cleaned[str(key)] = item
            continue
        raise ApiError(
            ErrorCode.VARIABLE_TYPE_INVALID,
            f"Variable '{key}' must hold a scalar; secrets and files are referenced, not embedded",
        )
    return cleaned


def _string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ApiError(ErrorCode.VALIDATION_ERROR, f"'{field}' must be a list of strings")
    return [item.strip() for item in value if item.strip()]


def _config_secret_keys(config: dict[str, Any]) -> list[str]:
    """`${secrets.name}` references anywhere in the config, so bindings and text cannot drift apart."""
    found: list[str] = []
    stack = [str(config)]
    while stack:
        text = stack.pop()
        start = text.find("${secrets.")
        if start < 0:
            continue
        rest = text[start + len("${secrets.") :]
        end = rest.find("}")
        if end <= 0:
            continue
        name = rest[:end].strip()
        if name and name not in found:
            found.append(name)
        stack.append(rest[end:])
    return found


def _version(value: Any) -> int | None:
    return None if value is None else int(value)


def _revision_payload(row: Any, *, current: str | None = None) -> dict[str, Any]:
    return {
        "environment_revision_id": row.id,
        "environment_id": row.environment_id,
        "version": int(row.version),
        "digest": row.digest,
        "config": dict(row.config or {}),
        "secret_bindings": dict(row.secret_bindings or {}),
        "current": row.id == current,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _environment_payload(environment: Any, *, revision: Any) -> dict[str, Any]:
    return {
        "environment_id": environment.id,
        "project_id": environment.project_id,
        "name": environment.environment_name,
        "row_version": int(environment.row_version or 1),
        "etag": f'W/"env-{environment.id}-{environment.row_version}"',
        "archived_at": environment.archived_at.isoformat() if environment.archived_at else None,
        "current_revision": _revision_payload(revision, current=environment.current_revision_id) if revision else None,
    }
