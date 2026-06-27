import base64
import uuid

import pytest
from fastapi import HTTPException

from app.core.deps import require_super_admin
from app.core.encryption import decrypt_secret, encrypt_secret, is_encrypted_secret
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    get_totp_provisioning_uri,
    get_totp_qr_code_data_uri,
    hash_password,
    verify_password,
)


def test_tokens_include_unique_jti_claims():
    subject = str(uuid.uuid4())

    first = create_access_token({"sub": subject})
    second = create_access_token({"sub": subject})

    first_payload = decode_token(first)
    second_payload = decode_token(second)

    assert first != second
    assert first_payload is not None
    assert second_payload is not None
    assert first_payload["jti"] != second_payload["jti"]
    assert first_payload["type"] == "access"


def test_refresh_token_has_refresh_type_and_jti():
    token = create_refresh_token({"sub": str(uuid.uuid4())})
    payload = decode_token(token)

    assert payload is not None
    assert payload["type"] == "refresh"
    assert uuid.UUID(payload["jti"])


def test_argon2_password_hash_round_trip():
    encoded = hash_password("correct horse battery staple")

    assert encoded.startswith("$argon2id$")
    assert verify_password("correct horse battery staple", encoded) is True
    assert verify_password("wrong password", encoded) is False


def test_totp_qr_code_data_uri_contains_valid_png():
    uri = get_totp_provisioning_uri("JBSWY3DPEHPK3PXP", "user@example.com")
    qr_code_data_uri = get_totp_qr_code_data_uri(uri)
    prefix = "data:image/png;base64,"

    assert uri.startswith("otpauth://totp/")
    assert qr_code_data_uri.startswith(prefix)
    png_bytes = base64.b64decode(qr_code_data_uri[len(prefix):], validate=True)
    assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n")


def test_sensitive_secret_encryption_round_trip_and_legacy_detection():
    secret = "JBSWY3DPEHPK3PXP"

    encrypted = encrypt_secret(secret)

    assert encrypted != secret
    assert is_encrypted_secret(encrypted) is True
    assert decrypt_secret(encrypted) == secret
    assert is_encrypted_secret(secret) is False
    assert decrypt_secret(secret) == secret


@pytest.mark.asyncio
async def test_platform_admin_guard_rejects_tenant_admin():
    tenant_admin = type("TenantAdmin", (), {"is_super_admin": False})()

    with pytest.raises(HTTPException) as exc:
        await require_super_admin(tenant_admin)

    assert exc.value.status_code == 403
