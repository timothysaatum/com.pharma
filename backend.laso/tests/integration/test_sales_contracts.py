"""
Integration tests for various price contract types during sale processing.
"""

import pytest
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
import uuid
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sales.sales_model import Sale, SaleItem
from app.models.inventory.branch_inventory import BranchInventory, DrugBatch
from app.models.inventory.inventory_model import Drug
from app.models.pharmacy.pharmacy_model import Branch, Organization
from app.models.customer.customer_model import Customer
from app.models.user.user_model import User
from app.models.pricing.pricing_model import PriceContract, PriceContractItem
from app.schemas.sales_schemas import SaleCreate, SaleItemCreate
from app.services.sales.sales_service import SalesService


@pytest.mark.asyncio
class TestPriceContractsIntegration:
    """Test suite for processing sales with different price contracts."""

    async def setup_sale_data(self, db, setup_test_data, contract_type, discount_pct=Decimal("10.00")):
        org, branch, user, drugs, customer = setup_test_data

        # Create contract
        contract = PriceContract(
            id=uuid.uuid4(),
            organization_id=org.id,
            contract_code=f"TEST-{contract_type.upper()}",
            contract_name=f"Test {contract_type.capitalize()} Contract",
            contract_type=contract_type,
            discount_percentage=float(discount_pct),
            effective_from=date.today() - timedelta(days=1),
            status="active",
            is_active=True,
            created_by=user.id,
        )
        db.add(contract)

        # Setup inventory and batch
        batch = DrugBatch(
            id=uuid.uuid4(),
            drug_id=drugs[0].id,
            branch_id=branch.id,
            batch_number="BATCH001",
            expiry_date=date.today() + timedelta(days=365),
            quantity=100,
            remaining_quantity=100,
        )
        db.add(batch)

        inventory = BranchInventory(
            id=uuid.uuid4(),
            branch_id=branch.id,
            drug_id=drugs[0].id,
            quantity=100,
            reserved_quantity=0,
            selling_price=Decimal("100.00"),
        )
        db.add(inventory)
        await db.commit()

        return contract

    async def test_insurance_contract_processing(self, db: AsyncSession, setup_test_data):
        """Test insurance contract with copay and covered amounts."""
        org, branch, user, drugs, customer = setup_test_data
        contract = await self.setup_sale_data(db, setup_test_data, "insurance", Decimal("20.00"))

        # Add copay percentage to contract
        contract.copay_percentage = 20.00
        await db.commit()

        sale_data = SaleCreate(
            branch_id=branch.id,
            price_contract_id=contract.id,
            customer_id=customer.id,
            items=[
                SaleItemCreate(
                    drug_id=drugs[0].id,
                    quantity=1,
                )
            ],
            payment_method="insurance",
            insurance_verified=True,
            insurance_claim_number="CLAIM-001",
        )

        response = await SalesService.process_sale(db, sale_data, user)
        sale = response.sale

        # Price=100, Discount=20% -> Total=80
        # Copay=20% of 80 -> 16
        # Insurance Covered = 80 - 16 = 64
        assert sale.total_amount == Decimal("80.00")
        assert sale.patient_copay_amount == Decimal("16.00")
        assert sale.insurance_covered_amount == Decimal("64.00")
        assert sale.insurance_claim_number == "CLAIM-001"

    async def test_corporate_contract_processing(self, db: AsyncSession, setup_test_data):
        """Test corporate contract processing."""
        org, branch, user, drugs, customer = setup_test_data
        contract = await self.setup_sale_data(db, setup_test_data, "corporate", Decimal("15.00"))

        sale_data = SaleCreate(
            branch_id=branch.id,
            price_contract_id=contract.id,
            customer_id=customer.id,
            items=[
                SaleItemCreate(
                    drug_id=drugs[0].id,
                    quantity=2,
                )
            ],
            payment_method="credit",
        )

        response = await SalesService.process_sale(db, sale_data, user)
        sale = response.sale

        # Price=100*2=200, Discount=15% of 200 = 30 -> Total=170
        assert sale.total_amount == Decimal("170.00")
        assert sale.total_discount_amount == Decimal("30.00")

    async def test_staff_contract_processing(self, db: AsyncSession, setup_test_data):
        """Test staff contract processing."""
        org, branch, user, drugs, customer = setup_test_data
        contract = await self.setup_sale_data(db, setup_test_data, "staff", Decimal("50.00"))

        sale_data = SaleCreate(
            branch_id=branch.id,
            price_contract_id=contract.id,
            customer_id=customer.id,
            items=[
                SaleItemCreate(
                    drug_id=drugs[0].id,
                    quantity=1,
                )
            ],
            payment_method="cash",
        )

        response = await SalesService.process_sale(db, sale_data, user)
        assert response.sale.total_amount == Decimal("50.00")

    async def test_wholesale_contract_processing(self, db: AsyncSession, setup_test_data):
        """Test wholesale contract requires registered customer."""
        org, branch, user, drugs, customer = setup_test_data
        contract = await self.setup_sale_data(db, setup_test_data, "wholesale", Decimal("25.00"))

        # Test without customer fails
        sale_data_no_cust = SaleCreate(
            branch_id=branch.id,
            price_contract_id=contract.id,
            customer_name="Walk-in",
            items=[SaleItemCreate(drug_id=drugs[0].id, quantity=10)],
            payment_method="cash",
        )

        with pytest.raises(Exception) as exc:
            await SalesService.process_sale(db, sale_data_no_cust, user)
        assert "Wholesale contracts require a registered customer" in str(exc.value)

    async def test_contract_with_fixed_price_override(self, db: AsyncSession, setup_test_data):
        """Test contract with a fixed price override for a specific drug."""
        org, branch, user, drugs, customer = setup_test_data
        contract = await self.setup_sale_data(db, setup_test_data, "standard", Decimal("0.00"))

        # Create fixed price override
        override = PriceContractItem(
            id=uuid.uuid4(),
            contract_id=contract.id,
            drug_id=drugs[0].id,
            fixed_price=75.00,
        )
        db.add(override)
        await db.commit()

        sale_data = SaleCreate(
            branch_id=branch.id,
            price_contract_id=contract.id,
            customer_id=customer.id,
            items=[SaleItemCreate(drug_id=drugs[0].id, quantity=2)],
            payment_method="cash",
        )

        response = await SalesService.process_sale(db, sale_data, user)
        # Fixed price 75 * 2 = 150 (instead of 100 * 2 = 200)
        assert response.sale.total_amount == Decimal("150.00")
        assert response.sale.total_discount_amount == Decimal("50.00")
