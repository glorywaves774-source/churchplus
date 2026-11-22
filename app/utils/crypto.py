"""Encryption of secrets stored in the database (e.g. two-step sign-in keys).

Values are stored as ``enc1:<Fernet token>``. The key comes from ``FIELD_ENCRYPTION_KEY``
(a Fernet key) or, if that is not set, is derived from ``SECRET_KEY``. Older plain values
(no prefix) are still read, and are encrypted the next time they are saved or when
``flask churchplus encrypt-secrets`` runs.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app

PREFIX = "enc1:"


def _fernet() -> Fernet:
    key = current_app.config.get("FIELD_ENCRYPTION_KEY")
    if not key:
        digest = hashlib.sha256(("churchplus-field-encryption:" + current_app.config["SECRET_KEY"]).encode()).digest()
        key = base64.urlsafe_b64encode(digest)
    return Fernet(key if isinstance(key, bytes) else key.encode())


def encrypt(value: str | None) -> str | None:
    if value is None or value == "" or value.startswith(PREFIX):
        return value
    return PREFIX + _fernet().encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    if not value or not value.startswith(PREFIX):
        return value                                   # legacy plain value
    try:
        return _fernet().decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken:
        current_app.logger.error("A stored secret could not be decrypted (encryption key changed?)")
        return None


def is_encrypted(value: str | None) -> bool:
    return bool(value) and value.startswith(PREFIX)
