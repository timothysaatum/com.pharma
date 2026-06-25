"""
Centralized exception handling utilities.

Provides:
- Database error translation (IntegrityError → user-friendly messages)
- HTTPException constructors for common error types
- Consistent error response builder
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, status
from sqlalchemy.exc import DataError, IntegrityError, SQLAlchemyError

logger = logging.getLogger(__name__)

# ── Constraint name → human-readable label ──────────────────────────────

CONSTRAINT_LABELS: Dict[str, str] = {
    # Organization
    "organizations_license_number_key": "License number",
    "organizations_tax_id_key": "Tax ID",
    "organizations_name_key": "Organization name",
    # Users
    "users_username_key": "Username",
    "users_email_key": "Email",
    # Branches
    "branches_code_key": "Branch code",
    # Inventory
    "drugs_sku_key": "SKU",
    "drug_categories_name_key": "Category name",
    "suppliers_name_key": "Supplier name",
    "insurance_providers_name_key": "Insurance provider name",
    "price_contracts_name_key": "Price contract name",
    # Misc
    "sync_queue_hash_key": "Sync record",
}


def _parse_constraint_name(constraint_name: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Parse a constraint name into (table, column) hints.
    e.g. 'organizations_license_number_key' → ('organizations', 'license_number')
    """
    if not constraint_name:
        return None, None
    parts = constraint_name.rsplit("_", 2)
    if len(parts) == 3 and parts[-1] == "key":
        return parts[0], parts[-2]
    return None, None


def _get_constraint_name(exc: IntegrityError) -> Optional[str]:
    """Extract the constraint name from an IntegrityError."""
    orig = getattr(exc, "orig", None)
    if orig is None:
        return None
    if hasattr(orig, "diag") and orig.diag is not None:
        return orig.diag.constraint_name
    return getattr(orig, "constraint_name", None)


def _get_duplicate_value(exc: IntegrityError) -> Optional[str]:
    """Extract the duplicate value from an IntegrityError detail message."""
    orig = getattr(exc, "orig", None)
    if orig is None:
        return None
    detail = getattr(orig, "detail", None) or str(orig)
    if "Key (" in detail and ")" in detail:
        try:
            start = detail.index("(") + 1
            end = detail.index(")")
            return detail[start:end].strip()
        except (ValueError, IndexError):
            return None
    return None


# ── Public API ─────────────────────────────────────────────────────────


def integrity_error_detail(exc: IntegrityError) -> str:
    """Translate an IntegrityError into a user-friendly detail message."""
    constraint_name = _get_constraint_name(exc)
    dup_value = _get_duplicate_value(exc)

    if constraint_name and constraint_name in CONSTRAINT_LABELS:
        label = CONSTRAINT_LABELS[constraint_name]
        if dup_value:
            return f"{label} '{dup_value}' is already in use."
        return f"{label} is already in use."

    if constraint_name:
        table, column = _parse_constraint_name(constraint_name)
        if column:
            label = column.replace("_", " ").title()
            if dup_value:
                message = f"{label} '{dup_value}' is already in use."
            else:
                message = f"{label} is already in use."
            logger.debug("Unmapped constraint: %s → %s", constraint_name, message)
            return message

    if dup_value:
        return f"Value '{dup_value}' is already in use."
    return "A record with the same unique value already exists."


def data_error_detail(exc: DataError) -> str:
    """Translate a DataError into a user-friendly detail message."""
    orig = getattr(exc, "orig", None)
    if orig:
        raw = str(orig)
        # Take the last meaningful part after the colon
        parts = raw.split(":")
        return f"Invalid data value: {parts[-1].strip()}" if len(parts) > 1 else raw
    return "The provided data is invalid for the expected format."


def raise_not_found(entity: str, identifier: Optional[str] = None) -> None:
    """Raise a 404 with a consistent message."""
    if identifier:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"{entity} '{identifier}' not found.")
    raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"{entity} not found.")


def raise_conflict(message: str) -> None:
    """Raise a 409 with a consistent message."""
    raise HTTPException(status.HTTP_409_CONFLICT, detail=message)


def raise_bad_request(message: str) -> None:
    """Raise a 400 with a consistent message."""
    raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=message)


def raise_forbidden(message: str = "You do not have permission to perform this action.") -> None:
    """Raise a 403 with a consistent message."""
    raise HTTPException(status.HTTP_403_FORBIDDEN, detail=message)


def raise_unauthorized(message: str = "Invalid authentication credentials.") -> None:
    """Raise a 401 with a consistent message."""
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=message)


def handle_integrity_error(exc: IntegrityError) -> HTTPException:
    """Convert an IntegrityError to a user-friendly HTTPException (409)."""
    detail = integrity_error_detail(exc)
    logger.warning("Integrity constraint violation: %s", detail)
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def handle_data_error(exc: DataError) -> HTTPException:
    """Convert a DataError to a user-friendly HTTPException (422)."""
    detail = data_error_detail(exc)
    logger.warning("Data error: %s", detail)
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def build_error_response(
    status_code: int,
    detail: str,
    request_id: Optional[str] = None,
    errors: Optional[List[Dict[str, Any]]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a consistent error response dictionary used across all handlers."""
    content: Dict[str, Any] = {
        "detail": detail,
    }
    if request_id:
        content["request_id"] = request_id
    if errors:
        content["errors"] = errors
    if extra:
        content.update(extra)
    return content


def log_and_raise(
    exc: Exception,
    request_id: Optional[str] = None,
    path: Optional[str] = None,
) -> None:
    """Log an exception and re-raise as HTTPException if it's a known DB error."""
    if isinstance(exc, IntegrityError):
        raise handle_integrity_error(exc) from exc
    if isinstance(exc, DataError):
        raise handle_data_error(exc) from exc
    if isinstance(exc, HTTPException):
        raise
    logger.error(
        "Unhandled exception [%s] on %s: %s",
        request_id or "?",
        path or "?",
        str(exc),
        exc_info=True,
    )
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="An unexpected error occurred. Please try again later.",
    ) from exc
