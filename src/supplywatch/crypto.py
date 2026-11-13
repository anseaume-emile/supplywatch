"""Encrypts stored OAuth tokens when SECRET_KEY is set (Fernet, key derived with SHA-256)."""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet

from . import config

_PLAIN = "plain:"


def _fernet() -> Fernet | None:
    k = config.settings.secret_key
    if not k:
        return None
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(k.encode()).digest()))


def encrypt(text: str) -> str:
    f = _fernet()
    return f.encrypt(text.encode()).decode() if f else _PLAIN + text


def decrypt(blob: str) -> str:
    if blob.startswith(_PLAIN):
        return blob[len(_PLAIN) :]
    f = _fernet()
    if not f:
        raise ValueError("SECRET_KEY is not set but the stored value is encrypted")
    return f.decrypt(blob.encode()).decode()
