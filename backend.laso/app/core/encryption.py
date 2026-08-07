"""Application-level encryption for secrets stored in the database."""

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings


class SecretDecryptionError(RuntimeError):
    """Raised when a stored value is not a valid Fernet token for the active key.

    Callers that need to support a legacy plaintext-secret migration (e.g.
    pre-encryption TOTP secrets) must catch this explicitly and handle the
    fallback themselves — this module no longer does it implicitly for every
    caller of decrypt_secret().
    """


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
        raise SecretDecryptionError("Value is not a valid encrypted secret.") from None
