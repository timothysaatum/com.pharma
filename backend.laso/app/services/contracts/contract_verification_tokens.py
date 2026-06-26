from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional
import uuid

import jwt
from jwt import ExpiredSignatureError, InvalidTokenError

from app.core.config import get_settings


TOKEN_TYPE = "contract_verification"
TOKEN_TTL_MINUTES = 10


class ContractVerificationTokenError(ValueError):
    """Raised when a contract verification token is missing, expired, or invalid."""


def _normalize_drug_ids(drug_ids: Iterable[uuid.UUID]) -> list[str]:
    return sorted({str(drug_id) for drug_id in drug_ids})


def create_contract_verification_token(
    *,
    organization_id: uuid.UUID,
    contract_id: uuid.UUID,
    branch_id: uuid.UUID,
    customer_id: Optional[uuid.UUID],
    drug_ids: Iterable[uuid.UUID],
    user_id: uuid.UUID,
) -> tuple[str, datetime]:
    """Create a short-lived token proving server-side contract eligibility."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=TOKEN_TTL_MINUTES)
    payload = {
        "type": TOKEN_TYPE,
        "org_id": str(organization_id),
        "contract_id": str(contract_id),
        "branch_id": str(branch_id),
        "customer_id": str(customer_id) if customer_id else None,
        "drug_ids": _normalize_drug_ids(drug_ids),
        "user_id": str(user_id),
        "iat": now,
        "exp": expires_at,
        "jti": str(uuid.uuid4()),
    }
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return token, expires_at


def verify_contract_verification_token(
    *,
    token: Optional[str],
    organization_id: uuid.UUID,
    contract_id: uuid.UUID,
    branch_id: uuid.UUID,
    customer_id: Optional[uuid.UUID],
    drug_ids: Iterable[uuid.UUID],
    user_id: uuid.UUID,
) -> None:
    """Validate a token against the exact sale context it is being used for."""
    if not token:
        raise ContractVerificationTokenError(
            "Contract verification token is required. Re-verify the contract before processing the sale."
        )

    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
        )
    except ExpiredSignatureError as exc:
        raise ContractVerificationTokenError(
            "Contract verification has expired. Re-verify the contract before processing the sale."
        ) from exc
    except InvalidTokenError as exc:
        raise ContractVerificationTokenError(
            "Invalid contract verification token. Re-verify the contract before processing the sale."
        ) from exc

    expected = {
        "type": TOKEN_TYPE,
        "org_id": str(organization_id),
        "contract_id": str(contract_id),
        "branch_id": str(branch_id),
        "customer_id": str(customer_id) if customer_id else None,
        "drug_ids": _normalize_drug_ids(drug_ids),
        "user_id": str(user_id),
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise ContractVerificationTokenError(
                "Contract verification token does not match this sale. Re-verify the contract before processing."
            )
