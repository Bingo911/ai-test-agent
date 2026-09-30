"""Transparent encryption of Text/JSON payloads without changing their database column types.

Indexes and identity/status columns stay queryable. JSON columns hold a JSON string envelope;
existing plaintext JSON/text can be read and migrated with scripts/encrypt_storage.py.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import JSON, Text
from sqlalchemy.types import TypeDecorator

from ..services.storage_crypto import ENVELOPE_PREFIX, cipher_for_dialect


class EncryptedText(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect) -> str | None:
        if value is None:
            return None
        return cipher_for_dialect(dialect).encrypt(value.encode("utf-8")).decode("ascii")

    def process_result_value(self, value: str | None, dialect) -> str | None:
        if value is None:
            return None
        return cipher_for_dialect(dialect).decrypt(value.encode("utf-8")).decode("utf-8")


class EncryptedJSON(TypeDecorator):
    impl = JSON
    cache_ok = True

    def process_bind_param(self, value: Any, dialect) -> str | None:
        if value is None:
            return None
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return cipher_for_dialect(dialect).encrypt(data).decode("ascii")

    def process_result_value(self, value: Any, dialect) -> Any:
        if not isinstance(value, str) or not value.startswith(ENVELOPE_PREFIX.decode("ascii")):
            return value
        return json.loads(cipher_for_dialect(dialect).decrypt(value.encode("ascii")))
