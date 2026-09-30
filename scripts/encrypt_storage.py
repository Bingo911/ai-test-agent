"""Backfill legacy payloads and local objects with authenticated encryption.

Run offline with the same SECRET_MASTER_KEY as the API/workers. Column types and object keys do not
change; already encrypted values are skipped, so an interrupted migration can be run again.
"""

from __future__ import annotations

import json
from pathlib import Path

from backend.app.config import get_settings
from backend.app.db.base import Base, get_database
from backend.app.db.encrypted_types import EncryptedJSON, EncryptedText
from backend.app.services.object_store import EncryptedObjectStore, LocalObjectStore, S3ObjectStore
from backend.app.services.storage_crypto import ENVELOPE_PREFIX
from sqlalchemy import Text, cast, select, update


def migrate_database(database, *, metadata=None) -> int:
    if metadata is None:
        from backend.app.db import models  # noqa: F401 (register the payload tables)

    changed = 0
    tables = (metadata or Base.metadata).sorted_tables
    prefix = ENVELOPE_PREFIX.decode("ascii")
    for table in tables:
        columns = [col for col in table.columns if isinstance(col.type, (EncryptedText, EncryptedJSON))]
        if not columns:
            continue
        last_id = ""
        while True:
            with database.engine.begin() as connection:
                rows = (
                    connection.execute(
                        select(table.c.id, *(cast(col, Text).label(col.name) for col in columns))
                        .where(table.c.id > last_id)
                        .order_by(table.c.id)
                        .limit(200)
                    )
                    .mappings()
                    .all()
                )
                for row in rows:
                    values = {}
                    for col in columns:
                        value = row[col.name]
                        if value is None:
                            continue
                        if isinstance(col.type, EncryptedJSON):
                            value = json.loads(value)
                        if value is None:
                            continue
                        if isinstance(value, str) and value.startswith(prefix):
                            continue
                        values[col.name] = value
                    if values:
                        connection.execute(update(table).where(table.c.id == row["id"]).values(**values))
                        changed += 1
                if len(rows) < 200:
                    break
                last_id = rows[-1]["id"]
    return changed


def migrate_local_objects(settings) -> int:
    raw = LocalObjectStore(settings.local_store_dir)
    root: Path = raw.root
    encrypted = EncryptedObjectStore(raw, settings)
    changed = 0
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix == ".part":
            continue
        key = path.relative_to(root).as_posix()
        if raw.read_prefix(key, max_bytes=len(ENVELOPE_PREFIX)) == ENVELOPE_PREFIX:
            continue
        encrypted.put_bytes(key, raw.read_bytes(key, max_bytes=settings.artifact_max_bytes), "application/octet-stream")
        changed += 1
    return changed


def migrate_s3_objects(settings) -> int:
    raw = S3ObjectStore(settings)
    encrypted = EncryptedObjectStore(raw, settings)
    changed = 0
    pages = raw.client.get_paginator("list_objects_v2").paginate(Bucket=raw.bucket, Prefix="tenants/")
    for page in pages:
        for item in page.get("Contents", []):
            key = item["Key"]
            if key.endswith(("/", ".part")):
                continue
            # A full bounded read also handles empty objects (S3 rejects their byte ranges).
            # Allow the envelope overhead so a previously migrated object at the limit is skipped.
            payload = raw.read_bytes(key, max_bytes=((settings.artifact_max_bytes + 256) * 4 // 3) + 128)
            if payload.startswith(ENVELOPE_PREFIX):
                continue
            media_type = raw.client.head_object(Bucket=raw.bucket, Key=key).get(
                "ContentType", "application/octet-stream"
            )
            encrypted.put_bytes(key, payload, media_type)
            changed += 1
    return changed


def main() -> None:
    settings = get_settings()
    database = get_database()
    rows = migrate_database(database)
    objects = migrate_local_objects(settings) if settings.object_store == "local" else migrate_s3_objects(settings)
    print(f"Encrypted {rows} legacy database rows and {objects} objects.")


if __name__ == "__main__":
    main()
