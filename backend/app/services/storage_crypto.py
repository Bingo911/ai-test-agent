"""Authenticated encryption for stored test payloads; the master key stays outside stored data."""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

from ..config import Settings, get_settings

ENVELOPE_PREFIX = b"AITA-ENC-v1:"


class StorageCipher:
    def __init__(self, settings: Settings) -> None:
        material = settings.secret_master_key
        if not material:
            if not settings.is_development:
                raise ValueError("secret_master_key is required for storage encryption")
            path = settings.data_dir / "keys" / "master.key"
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                # Publish a complete key atomically: concurrent API/worker startup cannot overwrite it.
                with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
                    temporary = Path(handle.name)
                    handle.write(Fernet.generate_key())
                try:
                    temporary.chmod(0o600)
                    with contextlib.suppress(FileExistsError):
                        os.link(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
            material = path.read_text(encoding="ascii").strip()
        self.fernet = Fernet(material.encode("ascii"))

    def encrypt(self, data: bytes) -> bytes:
        return ENVELOPE_PREFIX + self.fernet.encrypt(data)

    def decrypt(self, data: bytes) -> bytes:
        # Legacy objects remain readable until the explicit, idempotent migration is run.
        if not data.startswith(ENVELOPE_PREFIX):
            return data
        return self.fernet.decrypt(data[len(ENVELOPE_PREFIX) :])


def cipher_for_dialect(dialect) -> StorageCipher:
    cipher = getattr(dialect, "storage_cipher", None)
    if cipher is None:
        cipher = StorageCipher(get_settings())
        dialect.storage_cipher = cipher
    return cipher
