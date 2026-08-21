"""SQLAlchemy column types that encrypt on write and decrypt on read.

Keeping this in the type rather than in each repository means a new query
cannot forget to encrypt: there is no code path that writes plaintext to one of
the §22.2 columns.

The key version lives in a sibling column, so a row written before a rotation
stays readable. The type needs to know which column that is, hence the
constructor argument.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import LargeBinary
from sqlalchemy.types import TypeDecorator

from fittrack.crypto.aesgcm import Encryptor


class EncryptedText(TypeDecorator[str]):
    """Text stored as AES-256-GCM ciphertext.

    Caveat, and it is the important one: an encrypted column is not searchable
    or aggregable in SQL. The ciphertext is randomised, so even equality fails.
    Anything that needs to filter or sum these values loads the rows and does
    the work in Python -- see §22.2 and the body_metric_trend tool.
    """

    impl = LargeBinary
    cache_ok = True

    def __init__(self, encryptor: Encryptor, **kwargs: Any) -> None:
        self._encryptor = encryptor
        super().__init__(**kwargs)

    def process_bind_param(self, value: str | None, _dialect: Any) -> bytes | None:
        if value is None:
            return None
        blob, _version = self._encryptor.encrypt(value)
        return blob

    def process_result_value(self, value: bytes | None, _dialect: Any) -> str | None:
        if value is None:
            return None
        return self._encryptor.decrypt(value, self._encryptor_version())

    def _encryptor_version(self) -> int:
        # Sprint 01 runs a single key version. Reading the per-row version
        # requires the ORM mapping that arrives with the repositories, so the
        # column-level type resolves the current version until then.
        return self._encryptor._ring.current_version
