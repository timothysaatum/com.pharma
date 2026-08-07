import base64
import uuid

import pyotp
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_super_admin
from app.core.encryption import (
    decrypt_secret,
    encrypt_secret,
    is_encrypted_secret,
    SecretDecryptionError,
)
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

    # decrypt_secret() must no longer silently return the raw value for
    # non-Fernet input — that plaintext-fallback behavior is a caller-side
    # concern (see auth_service.py's legacy 2FA-secret migration), not
    # something the shared crypto primitive should do for every caller.
    with pytest.raises(SecretDecryptionError):
        decrypt_secret(secret)


@pytest.mark.asyncio
async def test_platform_admin_guard_rejects_tenant_admin():
    tenant_admin = type("TenantAdmin", (), {"is_super_admin": False})()

    with pytest.raises(HTTPException) as exc:
        await require_super_admin(tenant_admin)

    assert exc.value.status_code == 403


# ── Caller-side legacy-secret fallback (auth_service.py) ───────────────────
# decrypt_secret() itself now raises SecretDecryptionError instead of
# quietly returning raw input. auth_service.py's TOTP-verification call
# sites must still support users whose two_factor_secret predates the
# ENCRYPTION_KEY rollout, so login continues to work for them and the
# secret is transparently re-encrypted after a successful proof.

@pytest.mark.asyncio
async def test_login_with_legacy_plaintext_totp_secret_still_authenticates(
    db: AsyncSession,
):
    from app.models.pharmacy.pharmacy_model import Organization
    from app.models.user.user_model import User
    from app.schemas.user_schema import LoginRequest
    from app.services.auth.auth_service import AuthService

    org = Organization(
        id=uuid.uuid4(),
        name="Legacy 2FA Org",
        type="pharmacy",
        tax_id="LEGACY-2FA",
    )
    db.add(org)
    await db.flush()

    raw_totp_secret = pyotp.random_base32()
    user = User(
        id=uuid.uuid4(),
        organization_id=org.id,
        username="legacy_2fa_user",
        email="legacy2fa@pharmacy.test",
        password_hash=hash_password("CorrectHorseBattery1!"),
        full_name="Legacy 2FA User",
        is_super_admin=False,
        is_active=True,
        must_change_password=False,
        two_factor_enabled=True,
        # Pre-ENCRYPTION_KEY-rollout row: stored as plaintext, not a Fernet
        # token. decrypt_secret() raises SecretDecryptionError on this, and
        # AuthService.authenticate_user() must fall back to using it as-is.
        two_factor_secret=raw_totp_secret,
    )
    db.add(user)
    await db.commit()

    valid_code = pyotp.TOTP(raw_totp_secret).now()
    login_data = LoginRequest(
        username="legacy_2fa_user",
        password="CorrectHorseBattery1!",
        totp_code=valid_code,
    )

    authenticated_user, access_token, refresh_token = await AuthService.authenticate_user(
        db, login_data, ip_address="127.0.0.1", user_agent="pytest"
    )

    assert authenticated_user.id == user.id
    assert access_token
    assert refresh_token

    # The legacy plaintext secret must have been transparently migrated to
    # an encrypted value after the successful TOTP proof.
    assert authenticated_user.two_factor_secret != raw_totp_secret
    assert is_encrypted_secret(authenticated_user.two_factor_secret) is True
    assert decrypt_secret(authenticated_user.two_factor_secret) == raw_totp_secret
