from functools import lru_cache

from app.core.config import get_settings
from cryptography.fernet import Fernet


@lru_cache
def _fernet() -> Fernet:
    return Fernet(get_settings().encryption_key.encode("utf-8"))


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
