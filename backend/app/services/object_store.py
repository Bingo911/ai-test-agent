"""Object storage for evidence and attachments (§11.4).

Local filesystem for development, S3-compatible for production.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..config import Settings
from ..domain.errors import ApiError, ErrorCode
from ..observability import get_logger

log = get_logger(__name__)

_KEY_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def normalize_object_key(key: str) -> str:
    """Server-generated keys only; reject traversal, absolute paths and empty segments."""
    if not key or key.startswith("/") or ".." in key.split("/"):
        raise ApiError(ErrorCode.VALIDATION_ERROR, "Object key must be a server-generated relative path")
    segments = key.split("/")
    if not segments or any(not segment or not _KEY_SEGMENT.match(segment) for segment in segments):
        raise ApiError(ErrorCode.VALIDATION_ERROR, f"Object key '{key}' contains an unsafe segment")
    return "/".join(segments)


def safe_filename(name: str, *, limit: int = 120) -> str:
    cleaned = _SAFE.sub("_", Path(name or "file").name).strip("._") or "file"
    return cleaned[:limit]


@dataclass
class StoredObject:
    key: str
    size: int
    sha256: str
    media_type: str


class ObjectStore(Protocol):
    def put_bytes(self, key: str, data: bytes, media_type: str) -> StoredObject: ...

    def put_file(self, key: str, path: Path, media_type: str) -> StoredObject: ...

    def read_bytes(self, key: str, *, max_bytes: int | None = None) -> bytes: ...

    def local_path(self, key: str) -> Path | None: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...


class LocalObjectStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        target = (self.root / normalize_object_key(key)).resolve()
        if not str(target).startswith(str(self.root.resolve())):
            raise ApiError(ErrorCode.VALIDATION_ERROR, "Object key escapes the storage root")
        return target

    def put_bytes(self, key: str, data: bytes, media_type: str) -> StoredObject:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".part")
        temporary.write_bytes(data)
        temporary.replace(path)
        return StoredObject(
            key=key, size=len(data), sha256="sha256:" + hashlib.sha256(data).hexdigest(), media_type=media_type
        )

    def put_file(self, key: str, source: Path, media_type: str) -> StoredObject:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        with source.open("rb") as handle:
            while chunk := handle.read(1 << 20):
                digest.update(chunk)
                size += len(chunk)
        shutil.copyfile(source, path)
        return StoredObject(key=key, size=size, sha256="sha256:" + digest.hexdigest(), media_type=media_type)

    def read_bytes(self, key: str, *, max_bytes: int | None = None) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise ApiError(ErrorCode.NOT_FOUND, "Artifact object is missing")
        data = path.read_bytes()
        if max_bytes is not None and len(data) > max_bytes:
            raise ApiError(ErrorCode.PAYLOAD_TOO_LARGE, "Artifact exceeds the download limit")
        return data

    def local_path(self, key: str) -> Path | None:
        return self._path(key)

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()


class S3ObjectStore:
    def __init__(self, settings: Settings) -> None:
        import boto3
        from botocore.config import Config

        self.bucket = settings.object_store_bucket
        client_kwargs = {"region_name": settings.object_store_region, "config": Config(signature_version="s3v4")}
        if settings.object_store_endpoint:
            client_kwargs["endpoint_url"] = settings.object_store_endpoint
        if settings.object_store_access_key:
            client_kwargs["aws_access_key_id"] = settings.object_store_access_key
            client_kwargs["aws_secret_access_key"] = settings.object_store_secret_key
        self.client = boto3.client("s3", **client_kwargs)

    def put_bytes(self, key: str, data: bytes, media_type: str) -> StoredObject:
        clean = normalize_object_key(key)
        self.client.put_object(Bucket=self.bucket, Key=clean, Body=data, ContentType=media_type)
        return StoredObject(
            key=clean, size=len(data), sha256="sha256:" + hashlib.sha256(data).hexdigest(), media_type=media_type
        )

    def put_file(self, key: str, path: Path, media_type: str) -> StoredObject:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while chunk := handle.read(1 << 20):
                digest.update(chunk)
                size += len(chunk)
        self.client.upload_file(
            str(path), self.bucket, normalize_object_key(key), ExtraArgs={"ContentType": media_type}
        )
        return StoredObject(key=key, size=size, sha256="sha256:" + digest.hexdigest(), media_type=media_type)

    def read_bytes(self, key: str, *, max_bytes: int | None = None) -> bytes:
        try:
            range_header = f"bytes=0-{max_bytes - 1}" if max_bytes else None
            response = self.client.get_object(
                Bucket=self.bucket, Key=normalize_object_key(key), **({"Range": range_header} if range_header else {})
            )
            return response["Body"].read()
        except self.client.exceptions.NoSuchKey as exc:
            raise ApiError(ErrorCode.NOT_FOUND, "Artifact object is missing") from exc

    def local_path(self, key: str) -> Path | None:
        return None

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=normalize_object_key(key))
            return True
        except Exception:
            return False

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=normalize_object_key(key))


_store: ObjectStore | None = None


def get_object_store(settings: Settings | None = None) -> ObjectStore:
    global _store
    if _store is None:
        resolved = settings or Settings()
        resolved.ensure_dirs()
        _store = (
            S3ObjectStore(resolved) if resolved.object_store == "s3" else LocalObjectStore(resolved.local_store_dir)
        )
    return _store


def set_object_store(store: ObjectStore | None) -> None:
    global _store
    _store = store


def execution_object_key(tenant_id: str, project_id: str, execution_id: str, artifact_id: str, extension: str) -> str:
    extension = _SAFE.sub("", extension).lower().lstrip(".")
    suffix = f".{extension}" if extension else ""
    return f"tenants/{tenant_id}/projects/{project_id}/executions/{execution_id}/{artifact_id}{suffix}"


def attachment_object_key(tenant_id: str, project_id: str, attachment_id: str, filename: str) -> str:
    extension = Path(filename).suffix.lower()
    extension = _SAFE.sub("", extension)[:12]
    return f"tenants/{tenant_id}/projects/{project_id}/attachments/{attachment_id}{extension}"
