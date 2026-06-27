"""Application-level encryption for secrets stored in the database."""

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings


@lru_cache
def get_cipher_suite() -> Fernet:
    key = get_settings().ENCRYPTION_KEY
    if not key:
        raise RuntimeError(
            "ENCRYPTION_KEY is required for sensitive database fields."
        )
    return Fernet(key.encode())


def encrypt_secret(value: str) -> str:
    return get_cipher_suite().encrypt(value.encode()).decode()


def is_encrypted_secret(value: str) -> bool:
    """Return whether ``value`` is a valid Fernet token for the active key."""
    try:
        get_cipher_suite().decrypt(value.encode())
    except InvalidToken:
        return False
    return True


def decrypt_secret(value: str) -> str:
    try:
        return get_cipher_suite().decrypt(value.encode()).decode()
    except InvalidToken:
        # Backward-compatible read for pre-encryption rows. Callers rewrite the
        # value encrypted after successful verification.
        return value
