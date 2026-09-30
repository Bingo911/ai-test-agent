"""Versioned secrets with envelope encryption (§11.2, §14.3). Plaintext never leaves this module."""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import func, select

from ..config import Settings
from ..db.base import new_id, utcnow
from ..db.models import SecretVersion
from ..domain.enums import Sensitivity
from ..domain.errors import ApiError, ErrorCode
from .storage_crypto import StorageCipher


class SecretStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._fernet: Fernet | None = None

    @property
    def key_id(self) -> str:
        return "local-fernet-v1"

    def fernet(self) -> Fernet:
        if self._fernet is None:
            self._fernet = StorageCipher(self.settings).fernet
        return self._fernet

    def put(
        self, session, *, tenant_id: str, project_id: str, logical_name: str, value: str, created_by: str | None
    ) -> SecretVersion:
        if not value:
            raise ApiError(ErrorCode.VALIDATION_ERROR, "Secret value must not be empty")
        if len(value.encode("utf-8")) > 8192:
            raise ApiError(ErrorCode.PAYLOAD_TOO_LARGE, "Secret value exceeds 8 KiB")
        current = session.scalar(
            select(func.max(SecretVersion.version)).where(
                SecretVersion.tenant_id == tenant_id,
                SecretVersion.project_id == project_id,
                SecretVersion.logical_name == logical_name,
            )
        )
        record = SecretVersion(
            id=new_id(),
            tenant_id=tenant_id,
            project_id=project_id,
            logical_name=logical_name,
            version=int(current or 0) + 1,
            provider="local_fernet",
            ciphertext=self.fernet().encrypt(value.encode("utf-8")).decode("ascii"),
            key_id=self.key_id,
            status="ACTIVE",
            created_by=created_by,
        )
        session.add(record)
        session.flush()
        return record

    def latest(self, session, *, tenant_id: str, project_id: str, logical_name: str) -> SecretVersion | None:
        return session.scalar(
            select(SecretVersion)
            .where(
                SecretVersion.tenant_id == tenant_id,
                SecretVersion.project_id == project_id,
                SecretVersion.logical_name == logical_name,
            )
            .order_by(SecretVersion.version.desc())
            .limit(1)
        )

    def get(
        self, session, *, tenant_id: str, project_id: str, logical_name: str, version: int | None = None
    ) -> SecretVersion | None:
        if version is None:
            return self.latest(session, tenant_id=tenant_id, project_id=project_id, logical_name=logical_name)
        return session.scalar(
            select(SecretVersion).where(
                SecretVersion.tenant_id == tenant_id,
                SecretVersion.project_id == project_id,
                SecretVersion.logical_name == logical_name,
                SecretVersion.version == version,
            )
        )

    def reveal(self, session, *, tenant_id: str, project_id: str, logical_name: str, version: int | None = None) -> str:
        record = self.get(
            session, tenant_id=tenant_id, project_id=project_id, logical_name=logical_name, version=version
        )
        if record is None:
            raise ApiError(
                ErrorCode.SECRET_UNAVAILABLE,
                f"Secret '{logical_name}' is not defined in this project",
                details={"logical_name": logical_name},
            )
        if record.status != "ACTIVE":
            raise ApiError(
                ErrorCode.SECRET_VERSION_REVOKED,
                f"Secret '{logical_name}' version {record.version} has been revoked",
                details={"logical_name": logical_name, "version": record.version},
            )
        if not record.ciphertext:
            raise ApiError(ErrorCode.SECRET_UNAVAILABLE, f"Secret '{logical_name}' has no stored value")
        try:
            return self.fernet().decrypt(record.ciphertext.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ApiError(
                ErrorCode.SECRET_UNAVAILABLE,
                f"Secret '{logical_name}' could not be decrypted with the active key",
            ) from exc

    def list_for_project(self, session, *, tenant_id: str, project_id: str) -> list[dict]:
        """Names, versions and status only: a listing never carries ciphertext or plaintext (§14.3)."""
        rows = session.scalars(
            select(SecretVersion)
            .where(SecretVersion.tenant_id == tenant_id, SecretVersion.project_id == project_id)
            .order_by(SecretVersion.logical_name.asc(), SecretVersion.version.desc())
        ).all()
        return [
            {
                "secret_version_id": row.id,
                "logical_name": row.logical_name,
                "version": int(row.version),
                "provider": row.provider,
                "key_id": row.key_id,
                "status": row.status,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]

    def revoke(self, session, *, tenant_id: str, project_id: str, logical_name: str, version: int) -> None:
        record = self.get(
            session, tenant_id=tenant_id, project_id=project_id, logical_name=logical_name, version=version
        )
        if record is None:
            raise ApiError(ErrorCode.NOT_FOUND, f"Secret '{logical_name}' v{version} not found")
        record.status = "REVOKED"
        record.ciphertext = None
        record.updated_at = utcnow()
        session.flush()

    @staticmethod
    def evidence_mode_for(ir_payload: dict | None, *, requested: str | None = None) -> str:
        """IR referencing secrets, or an explicit request, forces SENSITIVE evidence (§10.4)."""
        if requested == Sensitivity.SENSITIVE.value:
            return Sensitivity.SENSITIVE.value

        def contains_secret(node) -> bool:
            if isinstance(node, dict):
                return node.get("kind") == "secret" or any(contains_secret(value) for value in node.values())
            if isinstance(node, list):
                return any(contains_secret(value) for value in node)
            return isinstance(node, str) and "${secrets." in node

        if contains_secret(ir_payload):
            return Sensitivity.SENSITIVE.value
        return requested or Sensitivity.NORMAL.value


_store: SecretStore | None = None


def get_secret_store(settings: Settings | None = None) -> SecretStore:
    global _store
    if _store is None:
        _store = SecretStore(settings or Settings())
    return _store
