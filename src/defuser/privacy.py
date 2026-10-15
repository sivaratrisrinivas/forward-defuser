"""Hashing of identifiers and encryption of the few reversible pointers we must keep."""

import hashlib
import hmac
import json

from cryptography.fernet import Fernet, InvalidToken

from defuser.config import get_settings


def hash_id(channel: str, raw_id: str | int) -> str:
    pepper = get_settings().hash_pepper.get_secret_value().encode()
    return hmac.new(pepper, f"{channel}:{raw_id}".encode(), hashlib.sha256).hexdigest()


def _fernet() -> Fernet:
    key = get_settings().data_key.get_secret_value()
    if not key:
        raise RuntimeError("DATA_KEY is not set; generate one with scripts/gen_keys.py")
    return Fernet(key.encode())


def encrypt_json(obj) -> bytes:
    return _fernet().encrypt(json.dumps(obj, separators=(",", ":")).encode())


def decrypt_json(blob: bytes):
    try:
        return json.loads(_fernet().decrypt(bytes(blob)))
    except InvalidToken as e:  # key rotated or data tampered
        raise ValueError("cannot decrypt stored reference") from e
