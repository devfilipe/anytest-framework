"""At-rest encryption for secret values, keyed by APP_SECRET.

Fernet (AES-128-CBC + HMAC) over a key derived from APP_SECRET. Secret values are never
stored or exported in plaintext. Set a strong APP_SECRET in production (32+ chars).
"""
from __future__ import annotations

import base64
import hashlib
import os

DEFAULT_SECRET = "dev-only-change-me-32byte-secret!!"


def app_secret() -> str:
    return os.environ.get("APP_SECRET") or DEFAULT_SECRET


def _key(app_secret: str) -> bytes:
    return base64.urlsafe_b64encode(hashlib.sha256(app_secret.encode()).digest())


class Cipher:
    def __init__(self, secret: str | None = None):
        from cryptography.fernet import Fernet
        self._f = Fernet(_key(secret or app_secret()))

    def enc(self, plaintext: str) -> str:
        return self._f.encrypt((plaintext or "").encode()).decode()

    def dec(self, token: str) -> str:
        try:
            return self._f.decrypt((token or "").encode()).decode()
        except Exception:
            return ""        # wrong key / corrupt → empty, never crash a run
