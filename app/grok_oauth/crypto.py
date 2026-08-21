from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.grok_oauth.constants import OWNER_SECRET_MIN_LENGTH

_ARGON2ID_HASH = re.compile(
    r"^\$argon2id\$v=\d+\$m=\d+,t=\d+,p=\d+\$[A-Za-z0-9+/=_-]+\$[A-Za-z0-9+/=_-]+$"
)
_SIGNING_INFO: Final[bytes] = b"agent-call-grok-oauth-signing-v1"
_STORAGE_INFO: Final[bytes] = b"agent-call-grok-oauth-storage-v1"
_MAC_INFO: Final[bytes] = b"agent-call-grok-oauth-mac-v1"
_SENTINEL_PLAINTEXT: Final[bytes] = b"agent-call-oauth-ok"

password_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
)


def is_argon2id_hash(value: str) -> bool:
    return bool(_ARGON2ID_HASH.fullmatch(value.strip()))


def hash_owner_secret(secret: str) -> str:
    if len(secret) < OWNER_SECRET_MIN_LENGTH:
        raise ValueError("owner secret is too short")
    return password_hasher.hash(secret)


def verify_owner_secret(*, secret: str, secret_hash: str) -> bool:
    try:
        return bool(password_hasher.verify(secret_hash, secret))
    except (VerifyMismatchError, InvalidHashError, ValueError, TypeError):
        return False


def _hkdf(*, material: str, salt: str, info: bytes, length: int = 32) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=length,
        salt=salt.encode("utf-8"),
        info=info,
    ).derive(material.encode("utf-8"))


def derive_signing_key(material: str, salt: str) -> bytes:
    return _hkdf(material=material, salt=salt, info=_SIGNING_INFO)


def derive_storage_fernet(material: str, salt: str) -> Fernet:
    import base64

    key = base64.urlsafe_b64encode(_hkdf(material=material, salt=salt, info=_STORAGE_INFO))
    return Fernet(key)


def derive_mac_key(material: str, salt: str) -> bytes:
    return _hkdf(material=material, salt=salt, info=_MAC_INFO)


def keyed_hash(value: str, key: bytes) -> str:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def encrypt_text(value: str, fernet: Fernet) -> str:
    return fernet.encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_text(token: str, fernet: Fernet) -> str:
    try:
        return fernet.decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError) as exc:
        raise RuntimeError("oauth storage decryption failed") from exc


def encrypt_sentinel(fernet: Fernet) -> str:
    return fernet.encrypt(_SENTINEL_PLAINTEXT).decode("ascii")


def verify_sentinel(token: str, fernet: Fernet) -> None:
    try:
        plaintext = fernet.decrypt(token.encode("ascii"))
    except (InvalidToken, ValueError, TypeError) as exc:
        raise RuntimeError(
            "GROK_MCP_OAUTH_STORAGE_ENCRYPTION_KEY is invalid for this database"
        ) from exc
    if plaintext != _SENTINEL_PLAINTEXT:
        raise RuntimeError("GROK_MCP_OAUTH_STORAGE_ENCRYPTION_KEY is invalid for this database")


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)
