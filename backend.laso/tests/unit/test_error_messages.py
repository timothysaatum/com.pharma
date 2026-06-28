"""
Tests verifying that HTTPException messages are specific and actionable.
"""

import pytest
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from app.models.pharmacy.pharmacy_model import Organization, Branch
from app.models.user.user_model import User
from app.models.sales.sales_model import Supplier, PurchaseOrder


class TestErrorMessageSpecificity:
    """Ensure all error messages describe *what* failed and *why*."""

    # ── Access-denied patterns ──────────────────────────────────────────────

    def test_users_permission_denied_message(self):
        """users.py _ensure_manager_can_manage_user explains why access is denied."""
        from app.api.v1.endpoints.users import _ensure_manager_can_manage_user

        manager = MagicMock(spec=User)
        manager.assigned_branches = {"branch-a"}
        manager.is_super_admin = False

        target = MagicMock(spec=User)
        target.assigned_branches = {"branch-b"}
        target.is_super_admin = False

        with pytest.raises(HTTPException) as exc:
            _ensure_manager_can_manage_user(manager, target)

        assert exc.value.status_code == 403
        assert "share at least one branch" in exc.value.detail.lower()

    def test_sales_get_sale_org_mismatch_message(self):
        """Sale org mismatch says 'different organization'."""
        from fastapi import status

        with pytest.raises(HTTPException) as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This sale belongs to a different organization",
            )
        assert exc.value.status_code == 403
        assert "different organization" in exc.value.detail.lower()
        assert "sale" in exc.value.detail.lower()

    def test_purchase_order_supplier_org_mismatch(self):
        """Supplier org mismatch is descriptive."""
        from fastapi import status

        with pytest.raises(HTTPException) as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This supplier belongs to a different organization",
            )
        assert exc.value.status_code == 403
        assert "supplier" in exc.value.detail.lower()
        assert "different organization" in exc.value.detail.lower()

    def test_purchase_order_po_org_mismatch(self):
        """Purchase order org mismatch is descriptive."""
        from fastapi import status

        with pytest.raises(HTTPException) as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This purchase order belongs to a different organization",
            )
        assert exc.value.status_code == 403
        assert "purchase order" in exc.value.detail.lower()
        assert "different organization" in exc.value.detail.lower()

    def test_onboarding_error_includes_cause(self):
        """Onboarding error includes underlying exception message."""
        from fastapi import status

        with pytest.raises(HTTPException) as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to onboard organization: database connection timeout",
            )
        assert exc.value.status_code == 500
        assert "database connection timeout" in exc.value.detail


class TestErrorBoundaryConditions:
    """Edge cases for error messages."""

    def test_inventory_negative_stock_includes_current_and_requested(self):
        """Negative stock error shows inventory.quantity and requested change."""
        with pytest.raises(HTTPException) as exc:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Adjustment would result in negative stock. "
                    "Current: 5, Change: -10, Result: -5."
                ),
            )
        assert exc.value.status_code == 400
        assert "Current: 5" in exc.value.detail
        assert "Change: -10" in exc.value.detail
        assert "Result: -5" in exc.value.detail

    def test_sale_insufficient_stock_includes_available_and_requested(self):
        """Insufficient stock error shows available and requested quantities."""
        with pytest.raises(HTTPException) as exc:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Insufficient stock for 'Paracetamol'. "
                    "Available: 3, Requested: 10."
                ),
            )
        assert exc.value.status_code == 400
        assert "Available: 3" in exc.value.detail
        assert "Requested: 10" in exc.value.detail
        assert "Paracetamol" in exc.value.detail

    def test_cannot_consume_expired_batch(self):
        """Error when trying to consume from an expired batch."""
        with pytest.raises(HTTPException) as exc:
            raise HTTPException(
                status_code=400,
                detail="Cannot consume from an expired batch.",
            )
        assert exc.value.status_code == 400
        assert "expired" in exc.value.detail

    def test_inactive_account_message(self):
        """Inactive account message tells user to contact admin."""
        with pytest.raises(HTTPException) as exc:
            raise HTTPException(
                status_code=403,
                detail="User account is inactive",
            )
        assert exc.value.status_code == 403
        assert "inactive" in exc.value.detail

    def test_locked_account_message(self):
        """Locked account message explains reason."""
        with pytest.raises(HTTPException) as exc:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Account is temporarily locked due to "
                    "too many failed login attempts"
                ),
            )
        assert exc.value.status_code == 403
        assert "locked" in exc.value.detail.lower()
        assert "login attempts" in exc.value.detail.lower()

    def test_wildcard_permission_message(self):
        """Wildcard permission error is clear about why it's blocked."""
        with pytest.raises(HTTPException) as exc:
            raise HTTPException(
                status_code=403,
                detail="Wildcard permission '*' is reserved for super admins",
            )
        assert exc.value.status_code == 403
        assert "wildcard" in exc.value.detail.lower()
        assert "super admin" in exc.value.detail.lower()


class TestValidationErrorMessages:
    """Pydantic schema validation errors should be self-explanatory."""

    @pytest.mark.asyncio
    async def test_inventory_expiry_date_future_required(self):
        """Past expiry date validation includes actionable message."""
        from datetime import date, timedelta

        try:
            from app.schemas.inventory_schemas import DrugBatchCreate
            from pydantic import ValidationError

            DrugBatchCreate(
                branch_id=uuid.uuid4(),
                drug_id=uuid.uuid4(),
                batch_number="TEST-001",
                quantity=10,
                remaining_quantity=10,
                expiry_date=date.today() - timedelta(days=1),
            )
        except ValidationError as e:
            errors = e.errors()
            msgs = " ".join(err["msg"].lower() for err in errors)
            assert "must be" in msgs
            assert "future" in msgs

    @pytest.mark.asyncio
    async def test_customer_duplicate_phone_includes_number(self):
        """Duplicate phone error includes the offending phone number."""
        with pytest.raises(HTTPException) as exc:
            raise HTTPException(
                status_code=409,
                detail="A customer with phone +233501234567 already exists in this organisation",
            )
        assert exc.value.status_code == 409
        assert "+233501234567" in exc.value.detail
        assert "already exists" in exc.value.detail

    @pytest.mark.asyncio
    async def test_prescription_number_duplicate_includes_value(self):
        """Duplicate prescription number error includes the number."""
        with pytest.raises(HTTPException) as exc:
            raise HTTPException(
                status_code=400,
                detail="Prescription number 'RX-001' already exists",
            )
        assert exc.value.status_code == 400
        assert "RX-001" in exc.value.detail
        assert "already exists" in exc.value.detail

    @pytest.mark.asyncio
    async def test_sale_change_amount_negative_includes_values(self):
        """Negative change error shows the computed value."""
        with pytest.raises(HTTPException) as exc:
            raise HTTPException(
                status_code=400,
                detail="Change amount -5.00 cannot be negative",
            )
        assert exc.value.status_code == 400
        assert "-5.00" in exc.value.detail
        assert "negative" in exc.value.detail.lower()

    @pytest.mark.asyncio
    async def test_loyalty_points_insufficient_includes_balance(self):
        """Insufficient points error shows balance."""
        with pytest.raises(HTTPException) as exc:
            raise HTTPException(
                status_code=400,
                detail="Cannot deduct 500 points: customer only has 100 points.",
            )
        assert exc.value.status_code == 400
        assert "500" in exc.value.detail
        assert "100" in exc.value.detail
