"""Small TOTP helpers for admin MFA; secrets are encrypted at rest."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken

from config import settings


def _fernet() -> Fernet:
    # Derive a dedicated encryption key instead of reusing the JWT key directly.
    key = hmac.new(
        settings.jwt_secret_key.encode("utf-8"),
        b"zafar-admin-totp-encryption-v1",
        hashlib.sha256,
    ).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_totp_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode("ascii")).decode("ascii")


def decrypt_totp_secret(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("ascii")
    except (InvalidToken, UnicodeError, ValueError) as exc:
        raise ValueError("Stored admin authenticator secret cannot be decrypted") from exc


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def provisioning_uri(secret: str, email: str, issuer: str = "StudyMW") -> str:
    label = quote(f"{issuer}:{email}", safe="")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"


def totp_step(secret: str, code: str, now: float | None = None, window: int = 1) -> int | None:
    if not isinstance(code, str) or len(code) != 6 or not code.isascii() or not code.isdigit():
        return None
    try:
        padding = "=" * ((8 - len(secret) % 8) % 8)
        key = base64.b32decode(secret.upper() + padding, casefold=True)
    except (ValueError, TypeError):
        return None

    current = int((time.time() if now is None else now) // 30)
    for step in range(current - window, current + window + 1):
        digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
        offset = digest[-1] & 0x0F
        binary = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
        expected = f"{binary % 1_000_000:06d}"
        if hmac.compare_digest(expected, code):
            return step
    return None
