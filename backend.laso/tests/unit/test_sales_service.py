"""
Comprehensive test suite for Sales Service
Tests cover: process_sale, refund_sale, cancel_sale operations
"""

import pytest
import pytest_asyncio
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.sales.sales_model import Sale, SaleItem, SaleItemBatchAllocation
from app.models.inventory.branch_inventory import BranchInventory, DrugBatch, StockAdjustment
from app.models.inventory.ledger import InventoryMovement
from app.models.inventory.inventory_model import Drug
from app.models.pharmacy.pharmacy_model import Branch, Organization
from app.models.customer.customer_model import Customer
from app.models.user.user_model import User
from app.models.prescriptions.prescription_model import Prescription
from app.models.pricing.pricing_model import PriceContract
from app.schemas.sales_schemas import SaleCreate, SaleItemCreate, RefundSaleRequest, SaleResponse
from app.services.contracts.contract_verification_tokens import create_contract_verification_token
from app.services.sales.sales_service import SalesService


@pytest.mark.asyncio
class TestProcessSale:
    """Test suite for processing sales transactions."""

    async def test_process_sale_basic(self, db: AsyncSession, setup_test_data):
        """Test basic sale processing with valid data."""
        org, branch, user, drugs, customer = setup_test_data
        
        # Create drug batch with sufficient stock
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
        
        # Create branch inventory
        inventory = BranchInventory(
            id=uuid.uuid4(),
            branch_id=branch.id,
            drug_id=drugs[0].id,
            quantity=100,
            reserved_quantity=0,
            selling_price=Decimal("50.00"),
        )
        db.add(inventory)
        await db.commit()

        # Create standard contract
        contract = PriceContract(
            id=uuid.uuid4(),
            organization_id=org.id,
            contract_code="STD",
            contract_name="Standard",
            contract_type="standard",
            effective_from=date.today() - timedelta(days=1),
            status="active",
            is_active=True,
            created_by=user.id,
        )
        db.add(contract)
        await db.commit()

        # Process sale
        sale_data = SaleCreate(
            branch_id=branch.id,
            price_contract_id=contract.id,
            customer_id=customer.id,
            items=[
                SaleItemCreate(
                    drug_id=drugs[0].id,
                    quantity=5,
                )
            ],
            payment_method="cash",
            amount_paid=Decimal("250.00"),
        )

        response = await SalesService.process_sale(db, sale_data, user)

        assert response.success
        assert response.sale.status == "completed"
        assert response.inventory_updated == 1
        assert response.sale.total_amount == Decimal("250.00")

    async def test_process_sale_records_batch_allocations_for_multi_batch_fefo(
        self,
        db: AsyncSession,
        setup_test_data,
    ):
        """A sale line spanning multiple batches should keep the full FEFO split."""
        org, branch, user, drugs, customer = setup_test_data
        drug = drugs[0]

        early_batch = DrugBatch(
            id=uuid.uuid4(),
            drug_id=drug.id,
            branch_id=branch.id,
            batch_number="EARLY",
            expiry_date=date.today() + timedelta(days=30),
            quantity=3,
            remaining_quantity=3,
            cost_price=Decimal("10.00"),
        )
        later_batch = DrugBatch(
            id=uuid.uuid4(),
            drug_id=drug.id,
            branch_id=branch.id,
            batch_number="LATER",
            expiry_date=date.today() + timedelta(days=365),
            quantity=10,
            remaining_quantity=10,
            cost_price=Decimal("11.00"),
        )
        inventory = BranchInventory(
            id=uuid.uuid4(),
            branch_id=branch.id,
            drug_id=drug.id,
            quantity=13,
            reserved_quantity=0,
            selling_price=Decimal("50.00"),
        )
        contract = PriceContract(
            id=uuid.uuid4(),
            organization_id=org.id,
            contract_code="STD-MULTI",
            contract_name="Standard",
            contract_type="standard",
            effective_from=date.today() - timedelta(days=1),
            status="active",
            is_active=True,
            created_by=user.id,
        )
        db.add_all([early_batch, later_batch, inventory, contract])
        await db.commit()

        sale_data = SaleCreate(
            branch_id=branch.id,
            price_contract_id=contract.id,
            customer_id=customer.id,
            items=[SaleItemCreate(drug_id=drug.id, quantity=5)],
            payment_method="cash",
            amount_paid=Decimal("250.00"),
        )

        response = await SalesService.process_sale(db, sale_data, user)

        assert response.success

        sale_item = await db.scalar(
            select(SaleItem).where(SaleItem.sale_id == response.sale.id)
        )
        allocations = (
            await db.execute(
                select(SaleItemBatchAllocation)
                .where(SaleItemBatchAllocation.sale_item_id == sale_item.id)
                .order_by(SaleItemBatchAllocation.batch_expiry_date)
            )
        ).scalars().all()
        assert [(a.batch_number, a.quantity) for a in allocations] == [
            ("EARLY", 3),
            ("LATER", 2),
        ]

        await db.refresh(early_batch)
        await db.refresh(later_batch)
        await db.refresh(inventory)
        assert early_batch.remaining_quantity == 0
        assert later_batch.remaining_quantity == 8
        assert inventory.quantity == 8

        movements = (
            await db.execute(
                select(InventoryMovement).where(
                    InventoryMovement.source_id == response.sale.id,
                    InventoryMovement.movement_type == "sale",
                )
            )
        ).scalars().all()
        assert len(movements) == 2
        assert sum(m.quantity_change for m in movements) == -5

    async def test_sale_response_items_count_from_loaded_items(self, db: AsyncSession, setup_test_data):
        """SaleResponse should populate items_count when sale items are present."""
        org, branch, user, drugs, customer = setup_test_data

        sale = Sale(
            id=uuid.uuid4(),
            organization_id=org.id,
            branch_id=branch.id,
            sale_number="SALE_COUNT_TEST",
            customer_id=customer.id,
            cashier_id=user.id,
            subtotal=Decimal("150.00"),
            discount_amount=Decimal("0.00"),
            tax_amount=Decimal("0.00"),
            total_amount=Decimal("150.00"),
            payment_method="cash",
            payment_status="completed",
            amount_paid=Decimal("150.00"),
            change_amount=Decimal("0.00"),
            status="completed",
        )

        sale_item = SaleItem(
            id=uuid.uuid4(),
            sale_id=sale.id,
            drug_id=drugs[0].id,
            drug_name=drugs[0].name,
            quantity=3,
            unit_price=Decimal("50.00"),
            subtotal=Decimal("150.00"),
            discount_percentage=Decimal("0.00"),
            discount_amount=Decimal("0.00"),
            tax_rate=Decimal("0.00"),
            tax_amount=Decimal("0.00"),
            total_price=Decimal("150.00"),
        )

        db.add_all([sale, sale_item])
        await db.commit()

        loaded_sale = await db.execute(
            select(Sale)
            .options(selectinload(Sale.items))
            .where(Sale.id == sale.id)
        )
        loaded_sale = loaded_sale.scalar_one()

        response = SaleResponse.model_validate(loaded_sale)

        assert response.items_count == 3

    async def test_process_sale_insufficient_stock(self, db: AsyncSession, setup_test_data):
        """Test sale fails when insufficient stock available."""
        org, branch, user, drugs, customer = setup_test_data
        
        # Create inventory with limited stock
        inventory = BranchInventory(
            id=uuid.uuid4(),
            branch_id=branch.id,
            drug_id=drugs[0].id,
            quantity=2,  # Only 2 units
            reserved_quantity=0,
            selling_price=Decimal("50.00"),
        )
        db.add(inventory)
        await db.commit()

        # Create standard contract
        contract = PriceContract(
            id=uuid.uuid4(),
            organization_id=org.id,
            contract_code="STD-FAIL",
            contract_name="Standard",
            contract_type="standard",
            effective_from=date.today() - timedelta(days=1),
            status="active",
            is_active=True,
            created_by=user.id,
        )
        db.add(contract)
        await db.commit()

        sale_data = SaleCreate(
            branch_id=branch.id,
            price_contract_id=contract.id,
            customer_id=customer.id,
            items=[
                SaleItemCreate(
                    drug_id=drugs[0].id,
                    quantity=5,  # Request 5, only 2 available
                )
            ],
            payment_method="cash",
            amount_paid=Decimal("250.00"),
        )

        with pytest.raises(Exception) as exc:
            await SalesService.process_sale(db, sale_data, user)
        
        assert "Insufficient stock" in str(exc.value)

    async def test_process_sale_rejects_expired_batch_only_stock(
        self,
        db: AsyncSession,
        setup_test_data,
    ):
        """Aggregate inventory is not sellable unless valid batches can cover it."""
        org, branch, user, drugs, customer = setup_test_data

        expired_batch = DrugBatch(
            id=uuid.uuid4(),
            drug_id=drugs[0].id,
            branch_id=branch.id,
            batch_number="EXPIRED001",
            expiry_date=date.today() - timedelta(days=1),
            quantity=10,
            remaining_quantity=10,
        )
        inventory = BranchInventory(
            id=uuid.uuid4(),
            branch_id=branch.id,
            drug_id=drugs[0].id,
            quantity=10,
            reserved_quantity=0,
            selling_price=Decimal("50.00"),
        )
        contract = PriceContract(
            id=uuid.uuid4(),
            organization_id=org.id,
            contract_code="STD-EXPIRED",
            contract_name="Standard",
            contract_type="standard",
            effective_from=date.today() - timedelta(days=1),
            status="active",
            is_active=True,
            created_by=user.id,
        )
        db.add_all([expired_batch, inventory, contract])
        await db.commit()

        sale_data = SaleCreate(
            branch_id=branch.id,
            price_contract_id=contract.id,
            customer_id=customer.id,
            items=[SaleItemCreate(drug_id=drugs[0].id, quantity=5)],
            payment_method="cash",
            amount_paid=Decimal("250.00"),
        )

        with pytest.raises(Exception) as exc:
            await SalesService.process_sale(db, sale_data, user)

        assert "Insufficient non-expired batch stock" in str(exc.value)

    async def test_process_sale_with_prescription(self, db: AsyncSession, setup_test_data):
        """Test sale of prescription drug requires valid prescription."""
        org, branch, user, drugs, customer = setup_test_data
        
        # Mark drug as requiring prescription
        drugs[0].requires_prescription = True
        
        # Create prescription
        rx = Prescription(
            id=uuid.uuid4(),
            customer_id=customer.id,
            prescription_number="RX-001",
            prescriber_name="Dr. Smith",
            prescriber_license="LIC-123",
            issue_date=date.today(),
            expiry_date=date.today() + timedelta(days=30),
            medications=[{"drug_id": str(drugs[0].id), "quantity": 10}],
            refills_allowed=2,
            refills_remaining=2,
            status="active",
            organization_id=org.id,
            branch_id=branch.id,
        )
        
        # Setup inventory and batch
        batch = DrugBatch(
            id=uuid.uuid4(),
            drug_id=drugs[0].id,
            branch_id=branch.id,
            batch_number="RX001",
            expiry_date=date.today() + timedelta(days=365),
            quantity=50,
            remaining_quantity=50,
        )
        
        inventory = BranchInventory(
            id=uuid.uuid4(),
            branch_id=branch.id,
            drug_id=drugs[0].id,
            quantity=50,
            reserved_quantity=0,
            selling_price=Decimal("100.00"),
        )
        
        db.add_all([rx, batch, inventory])
        await db.commit()

        # Create standard contract
        contract = PriceContract(
            id=uuid.uuid4(),
            organization_id=org.id,
            contract_code="STD-RX",
            contract_name="Standard",
            contract_type="standard",
            effective_from=date.today() - timedelta(days=1),
            status="active",
            is_active=True,
            created_by=user.id,
        )
        db.add(contract)
        await db.commit()

        sale_data = SaleCreate(
            branch_id=branch.id,
            price_contract_id=contract.id,
            customer_id=customer.id,
            items=[
                SaleItemCreate(
                    drug_id=drugs[0].id,
                    quantity=3,
                )
            ],
            payment_method="cash",
            amount_paid=Decimal("300.00"),
            prescription_id=rx.id,
        )

        response = await SalesService.process_sale(db, sale_data, user)
        assert response.success

    async def test_process_sale_rejects_prescription_drug_not_on_prescription(
        self,
        db: AsyncSession,
        setup_test_data,
    ):
        """An active prescription cannot authorize drugs that are not on it."""
        org, branch, user, drugs, customer = setup_test_data
        drugs[0].requires_prescription = True

        rx = Prescription(
            id=uuid.uuid4(),
            customer_id=customer.id,
            prescription_number="RX-MISMATCH",
            prescriber_name="Dr. Smith",
            prescriber_license="LIC-123",
            issue_date=date.today(),
            expiry_date=date.today() + timedelta(days=30),
            medications=[{"drug_id": str(drugs[1].id), "quantity": 10}],
            refills_allowed=2,
            refills_remaining=2,
            status="active",
            organization_id=org.id,
            branch_id=branch.id,
        )
        batch = DrugBatch(
            id=uuid.uuid4(),
            drug_id=drugs[0].id,
            branch_id=branch.id,
            batch_number="RX-MISMATCH-BATCH",
            expiry_date=date.today() + timedelta(days=365),
            quantity=50,
            remaining_quantity=50,
        )
        inventory = BranchInventory(
            id=uuid.uuid4(),
            branch_id=branch.id,
            drug_id=drugs[0].id,
            quantity=50,
            reserved_quantity=0,
            selling_price=Decimal("100.00"),
        )
        contract = PriceContract(
            id=uuid.uuid4(),
            organization_id=org.id,
            contract_code="STD-RX-MISMATCH",
            contract_name="Standard",
            contract_type="standard",
            effective_from=date.today() - timedelta(days=1),
            status="active",
            is_active=True,
            created_by=user.id,
        )
        db.add_all([rx, batch, inventory, contract])
        await db.commit()

        sale_data = SaleCreate(
            branch_id=branch.id,
            price_contract_id=contract.id,
            customer_id=customer.id,
            items=[SaleItemCreate(drug_id=drugs[0].id, quantity=3)],
            payment_method="cash",
            amount_paid=Decimal("300.00"),
            prescription_id=rx.id,
        )

        with pytest.raises(Exception) as exc:
            await SalesService.process_sale(db, sale_data, user)

        assert "does not include" in str(exc.value)

    async def test_process_sale_requires_server_contract_verification_token(
        self,
        db: AsyncSession,
        setup_test_data,
    ):
        """Client-side insurance_verified is not enough for verified contracts."""
        org, branch, user, drugs, customer = setup_test_data
        drug = drugs[0]
        batch = DrugBatch(
            id=uuid.uuid4(),
            drug_id=drug.id,
            branch_id=branch.id,
            batch_number="VERIFY-BATCH",
            expiry_date=date.today() + timedelta(days=365),
            quantity=20,
            remaining_quantity=20,
        )
        inventory = BranchInventory(
            id=uuid.uuid4(),
            branch_id=branch.id,
            drug_id=drug.id,
            quantity=20,
            reserved_quantity=0,
            selling_price=Decimal("50.00"),
        )
        contract = PriceContract(
            id=uuid.uuid4(),
            organization_id=org.id,
            contract_code="VERIFY-REQ",
            contract_name="Verified Contract",
            contract_type="standard",
            effective_from=date.today() - timedelta(days=1),
            status="active",
            is_active=True,
            requires_verification=True,
            created_by=user.id,
        )
        db.add_all([batch, inventory, contract])
        await db.commit()

        sale_data = SaleCreate(
            branch_id=branch.id,
            price_contract_id=contract.id,
            customer_id=customer.id,
            items=[SaleItemCreate(drug_id=drug.id, quantity=2)],
            payment_method="cash",
            amount_paid=Decimal("100.00"),
            insurance_verified=True,
        )

        with pytest.raises(Exception) as exc:
            await SalesService.process_sale(db, sale_data, user)

        assert "verification token is required" in str(exc.value)

    async def test_process_sale_accepts_contract_token_and_updates_metrics(
        self,
        db: AsyncSession,
        setup_test_data,
    ):
        """A valid token allows verified contracts and updates usage metrics."""
        org, branch, user, drugs, customer = setup_test_data
        drug = drugs[0]
        batch = DrugBatch(
            id=uuid.uuid4(),
            drug_id=drug.id,
            branch_id=branch.id,
            batch_number="VERIFY-OK-BATCH",
            expiry_date=date.today() + timedelta(days=365),
            quantity=20,
            remaining_quantity=20,
        )
        inventory = BranchInventory(
            id=uuid.uuid4(),
            branch_id=branch.id,
            drug_id=drug.id,
            quantity=20,
            reserved_quantity=0,
            selling_price=Decimal("50.00"),
        )
        contract = PriceContract(
            id=uuid.uuid4(),
            organization_id=org.id,
            contract_code="VERIFY-OK",
            contract_name="Verified Contract",
            contract_type="standard",
            discount_percentage=Decimal("10.00"),
            effective_from=date.today() - timedelta(days=1),
            status="active",
            is_active=True,
            requires_verification=True,
            created_by=user.id,
        )
        db.add_all([batch, inventory, contract])
        await db.commit()

        token, _ = create_contract_verification_token(
            organization_id=org.id,
            contract_id=contract.id,
            branch_id=branch.id,
            customer_id=customer.id,
            drug_ids=[drug.id],
            user_id=user.id,
        )
        sale_data = SaleCreate(
            branch_id=branch.id,
            price_contract_id=contract.id,
            customer_id=customer.id,
            items=[SaleItemCreate(drug_id=drug.id, quantity=2)],
            payment_method="cash",
            amount_paid=Decimal("90.00"),
            contract_verification_token=token,
        )

        response = await SalesService.process_sale(db, sale_data, user)

        assert response.success
        await db.refresh(contract)
        assert contract.total_transactions == 1
        assert Decimal(str(contract.total_discount_given)) == Decimal("10.00")

    async def test_process_sale_rejects_split_payment_total_mismatch(
        self,
        db: AsyncSession,
        setup_test_data,
    ):
        """Split payment details must reconcile to the computed amount due."""
        org, branch, user, drugs, customer = setup_test_data
        drug = drugs[0]
        batch = DrugBatch(
            id=uuid.uuid4(),
            drug_id=drug.id,
            branch_id=branch.id,
            batch_number="SPLIT-BATCH",
            expiry_date=date.today() + timedelta(days=365),
            quantity=20,
            remaining_quantity=20,
        )
        inventory = BranchInventory(
            id=uuid.uuid4(),
            branch_id=branch.id,
            drug_id=drug.id,
            quantity=20,
            reserved_quantity=0,
            selling_price=Decimal("50.00"),
        )
        contract = PriceContract(
            id=uuid.uuid4(),
            organization_id=org.id,
            contract_code="STD-SPLIT",
            contract_name="Standard",
            contract_type="standard",
            effective_from=date.today() - timedelta(days=1),
            status="active",
            is_active=True,
            created_by=user.id,
        )
        db.add_all([batch, inventory, contract])
        await db.commit()

        sale_data = SaleCreate(
            branch_id=branch.id,
            price_contract_id=contract.id,
            customer_id=customer.id,
            items=[SaleItemCreate(drug_id=drug.id, quantity=2)],
            payment_method="split",
            split_payment_details={
                "cash": Decimal("50.00"),
                "card": Decimal("40.00"),
            },
        )

        with pytest.raises(Exception) as exc:
            await SalesService.process_sale(db, sale_data, user)

        assert "Split payment total" in str(exc.value)

    async def test_process_sale_rx_drug_with_insufficient_stock_reports_both_errors(
        self,
        db: AsyncSession,
        setup_test_data,
    ):
        """An Rx-only drug with low stock AND no Rx reports BOTH errors."""
        org, branch, user, drugs, customer = setup_test_data
        drugs[0].requires_prescription = True

        inventory = BranchInventory(
            id=uuid.uuid4(),
            branch_id=branch.id,
            drug_id=drugs[0].id,
            quantity=1,
            reserved_quantity=0,
            selling_price=Decimal("50.00"),
        )
        batch = DrugBatch(
            id=uuid.uuid4(),
            drug_id=drugs[0].id,
            branch_id=branch.id,
            batch_number="BOTH-ERR",
            expiry_date=date.today() + timedelta(days=365),
            quantity=1,
            remaining_quantity=1,
        )
        contract = PriceContract(
            id=uuid.uuid4(),
            organization_id=org.id,
            contract_code="STD-BOTH",
            contract_name="Standard",
            contract_type="standard",
            effective_from=date.today() - timedelta(days=1),
            status="active",
            is_active=True,
            created_by=user.id,
        )
        db.add_all([inventory, batch, contract])
        await db.commit()

        sale_data = SaleCreate(
            branch_id=branch.id,
            price_contract_id=contract.id,
            customer_id=customer.id,
            items=[SaleItemCreate(drug_id=drugs[0].id, quantity=5)],
            payment_method="cash",
            amount_paid=Decimal("250.00"),
        )

        with pytest.raises(Exception) as exc:
            await SalesService.process_sale(db, sale_data, user)

        err_str = str(exc.value)
        assert "Insufficient stock" in err_str, (
            "Should report stock error even when Rx is also missing"
        )
        assert "requires a valid prescription" in err_str, (
            "Should also report prescription error"
        )

    async def test_process_sale_multiple_items_collect_all_errors(
        self,
        db: AsyncSession,
        setup_test_data,
    ):
        """Multiple items with different issues should report ALL errors."""
        org, branch, user, drugs, customer = setup_test_data
        drugs[0].requires_prescription = True  # Rx-only, no Rx → error
        # drug[1] is not Rx-only, but has no stock → error

        inventory_0 = BranchInventory(
            id=uuid.uuid4(),
            branch_id=branch.id,
            drug_id=drugs[0].id,
            quantity=10,
            reserved_quantity=0,
            selling_price=Decimal("50.00"),
        )
        batch_0 = DrugBatch(
            id=uuid.uuid4(),
            drug_id=drugs[0].id,
            branch_id=branch.id,
            batch_number="MULTI-0",
            expiry_date=date.today() + timedelta(days=365),
            quantity=10,
            remaining_quantity=10,
        )
        # drug[1] has inventory but very low qty
        inventory_1 = BranchInventory(
            id=uuid.uuid4(),
            branch_id=branch.id,
            drug_id=drugs[1].id,
            quantity=2,
            reserved_quantity=0,
            selling_price=Decimal("30.00"),
        )
        batch_1 = DrugBatch(
            id=uuid.uuid4(),
            drug_id=drugs[1].id,
            branch_id=branch.id,
            batch_number="MULTI-1",
            expiry_date=date.today() + timedelta(days=365),
            quantity=2,
            remaining_quantity=2,
        )
        contract = PriceContract(
            id=uuid.uuid4(),
            organization_id=org.id,
            contract_code="STD-MULTI-ERR",
            contract_name="Standard",
            contract_type="standard",
            effective_from=date.today() - timedelta(days=1),
            status="active",
            is_active=True,
            created_by=user.id,
        )
        db.add_all([inventory_0, batch_0, inventory_1, batch_1, contract])
        await db.commit()

        sale_data = SaleCreate(
            branch_id=branch.id,
            price_contract_id=contract.id,
            customer_id=customer.id,
            items=[
                SaleItemCreate(drug_id=drugs[0].id, quantity=3),
                SaleItemCreate(drug_id=drugs[1].id, quantity=5),
            ],
            payment_method="cash",
            amount_paid=Decimal("300.00"),
        )

        with pytest.raises(Exception) as exc:
            await SalesService.process_sale(db, sale_data, user)

        err_str = str(exc.value)
        assert "requires a valid prescription" in err_str, (
            "Item 0 (Rx-only) should report prescription error"
        )
        assert "Insufficient stock" in err_str, (
            "Item 1 should report insufficient stock error"
        )

    async def test_process_sale_stock_validated_before_rx_gate(
        self,
        db: AsyncSession,
        setup_test_data,
    ):
        """Rx drug that has neither stock nor Rx gets BOTH errors, not just Rx."""
        org, branch, user, drugs, customer = setup_test_data
        drugs[0].requires_prescription = True

        # Intentionally NO inventory and NO batch for this drug
        contract = PriceContract(
            id=uuid.uuid4(),
            organization_id=org.id,
            contract_code="STD-NO-INV",
            contract_name="Standard",
            contract_type="standard",
            effective_from=date.today() - timedelta(days=1),
            status="active",
            is_active=True,
            created_by=user.id,
        )
        db.add(contract)
        await db.commit()

        sale_data = SaleCreate(
            branch_id=branch.id,
            price_contract_id=contract.id,
            customer_id=customer.id,
            items=[SaleItemCreate(drug_id=drugs[0].id, quantity=3)],
            payment_method="cash",
            amount_paid=Decimal("150.00"),
        )

        with pytest.raises(Exception) as exc:
            await SalesService.process_sale(db, sale_data, user)

        err_str = str(exc.value)
        assert "No inventory record" in err_str, (
            "Stock validation should run (no inventory record)"
        )
        assert "requires a valid prescription" in err_str, (
            "Prescription validation should also run"
        )


@pytest.mark.asyncio
class TestRefundSale:
    """Test suite for refunding sales."""

    async def test_refund_sale_full(self, db: AsyncSession, setup_test_data, completed_sale):
        """Test full refund of a completed sale."""
        sale, user = completed_sale
        # Load items
        res = await db.execute(select(SaleItem).where(SaleItem.sale_id == sale.id))
        items = res.scalars().all()

        refund_data = RefundSaleRequest(
            reason="Customer returned item",
            items_to_refund=[
                {
                    "sale_item_id": items[0].id,
                    "quantity": items[0].quantity,
                    "reason": "Return",
                    "restock": True,
                }
            ],
            refund_amount=sale.total_amount,
            refund_method="cash",
            manager_approval_user_id=user.id,
        )

        response = await SalesService.refund_sale(db, sale.id, refund_data, user)

        assert response.success
        assert response.inventory_restored == 1
        assert response.refund_amount == sale.total_amount

    async def test_refund_sale_partial(self, db: AsyncSession, setup_test_data, completed_sale):
        """Test partial refund of specific items."""
        sale, user = completed_sale
        # Load items
        res = await db.execute(select(SaleItem).where(SaleItem.sale_id == sale.id))
        items = res.scalars().all()

        # Refund only half the quantity
        refund_qty = items[0].quantity // 2
        refund_amount = (items[0].total_price / items[0].quantity) * refund_qty

        refund_data = RefundSaleRequest(
            reason="Partial return",
            items_to_refund=[
                {
                    "sale_item_id": items[0].id,
                    "quantity": refund_qty,
                    "reason": "Return",
                    "restock": True,
                }
            ],
            refund_amount=refund_amount,
            refund_method="cash",
            manager_approval_user_id=user.id,
        )

        response = await SalesService.refund_sale(db, sale.id, refund_data, user)

        assert response.success
        assert response.refund_amount == refund_amount
        assert response.sale.status == "partially_refunded"
        assert response.sale.payment_status == "partial"
        assert response.sale.refund_amount == refund_amount

    async def test_refund_cannot_exceed_remaining_refundable_total(
        self,
        db: AsyncSession,
        setup_test_data,
        completed_sale,
    ):
        """Multiple partial refunds cannot exceed the original sale total."""
        sale, user = completed_sale
        res = await db.execute(select(SaleItem).where(SaleItem.sale_id == sale.id))
        item = res.scalars().one()

        first_refund = RefundSaleRequest(
            reason="First partial return",
            items_to_refund=[
                {
                    "sale_item_id": item.id,
                    "quantity": 4,
                    "reason": "Return",
                    "restock": True,
                }
            ],
            refund_amount=Decimal("200.00"),
            refund_method="cash",
            manager_approval_user_id=user.id,
        )
        await SalesService.refund_sale(db, sale.id, first_refund, user)

        second_refund = RefundSaleRequest(
            reason="Second partial return",
            items_to_refund=[
                {
                    "sale_item_id": item.id,
                    "quantity": 2,
                    "reason": "Return",
                    "restock": True,
                }
            ],
            refund_amount=Decimal("100.00"),
            refund_method="cash",
            manager_approval_user_id=user.id,
        )

        with pytest.raises(Exception) as exc:
            await SalesService.refund_sale(db, sale.id, second_refund, user)

        assert "cannot exceed sale total" in str(exc.value)

    async def test_refund_cannot_exceed_remaining_item_quantity(
        self,
        db: AsyncSession,
        setup_test_data,
        completed_sale,
    ):
        """Refund history is tracked per sale item, not only by total amount."""
        sale, user = completed_sale
        res = await db.execute(select(SaleItem).where(SaleItem.sale_id == sale.id))
        item = res.scalars().one()

        first_refund = RefundSaleRequest(
            reason="First partial return",
            items_to_refund=[
                {
                    "sale_item_id": item.id,
                    "quantity": 4,
                    "reason": "Return",
                    "restock": True,
                }
            ],
            refund_amount=Decimal("200.00"),
            refund_method="cash",
            manager_approval_user_id=user.id,
        )
        await SalesService.refund_sale(db, sale.id, first_refund, user)

        second_refund = RefundSaleRequest(
            reason="Second partial return",
            items_to_refund=[
                {
                    "sale_item_id": item.id,
                    "quantity": 2,
                    "reason": "Return",
                    "restock": True,
                }
            ],
            refund_amount=Decimal("50.00"),
            refund_method="cash",
            manager_approval_user_id=user.id,
        )

        with pytest.raises(Exception) as exc:
            await SalesService.refund_sale(db, sale.id, second_refund, user)

        assert "Remaining refundable quantity: 1" in str(exc.value)

    async def test_refund_exceeds_sale_total(self, db: AsyncSession, setup_test_data, completed_sale):
        """Test refund cannot exceed sale total."""
        sale, user = completed_sale
        # Load items
        res = await db.execute(select(SaleItem).where(SaleItem.sale_id == sale.id))
        items = res.scalars().all()

        refund_data = RefundSaleRequest(
            reason="Invalid refund",
            items_to_refund=[
                {
                    "sale_item_id": items[0].id,
                    "quantity": items[0].quantity,
                    "reason": "Return",
                    "restock": True,
                }
            ],
            refund_amount=sale.total_amount + Decimal("100.00"),  # Exceeds total
            refund_method="cash",
            manager_approval_user_id=user.id,
        )

        with pytest.raises(Exception) as exc:
            await SalesService.refund_sale(db, sale.id, refund_data, user)
        
        assert "cannot exceed" in str(exc.value)

    async def test_refund_restores_inventory(self, db: AsyncSession, completed_sale):
        """Test refund properly restores inventory."""
        sale, user = completed_sale
        # Load items
        res = await db.execute(select(SaleItem).where(SaleItem.sale_id == sale.id))
        items = res.scalars().all()

        # Get initial inventory
        initial_qty = items[0].quantity # Initial was 95, 5 were sold, total 100

        refund_data = RefundSaleRequest(
            reason="Test refund",
            items_to_refund=[
                {
                    "sale_item_id": items[0].id,
                    "quantity": items[0].quantity,
                    "reason": "Return",
                    "restock": True,
                }
            ],
            refund_amount=sale.total_amount,
            refund_method="cash",
            manager_approval_user_id=user.id,
        )

        await SalesService.refund_sale(db, sale.id, refund_data, user)

        # Check inventory increased
        updated_inventory_res = await db.execute(
            select(BranchInventory.quantity).where(BranchInventory.drug_id == items[0].drug_id)
        )
        updated_qty = updated_inventory_res.scalar()

        assert updated_qty == 95 + items[0].quantity


@pytest.mark.asyncio  
class TestInventoryDeduction:
    """Test suite for inventory deduction during sales."""

    async def test_fefo_batch_selection(self, db: AsyncSession):
        """Test FEFO (First Expire, First Out) batch selection for inventory."""
        # Create multiple batches with different expiry dates
        # The service should deduct from earliest expiring batch first
        pass

    async def test_inventory_reserved_during_sale(self, db: AsyncSession):
        """Test inventory is properly reserved during sale processing."""
        pass


# ─── Fixtures ───────────────────────────────────────────────────────────────


@pytest_asyncio.fixture(scope="function")
async def completed_sale(db: AsyncSession, setup_test_data):
    """Create a completed sale for testing refunds."""
    org, branch, user, drugs, customer = setup_test_data
    
    # Setup inventory and batch
    batch = DrugBatch(
        id=uuid.uuid4(),
        drug_id=drugs[0].id,
        branch_id=branch.id,
        batch_number="TEST001",
        expiry_date=date.today() + timedelta(days=365),
        quantity=100,
        remaining_quantity=95,
    )
    
    inventory = BranchInventory(
        id=uuid.uuid4(),
        branch_id=branch.id,
        drug_id=drugs[0].id,
        quantity=95,
        reserved_quantity=0,
        selling_price=Decimal("50.00"),
    )
    
    sale = Sale(
        id=uuid.uuid4(),
        organization_id=org.id,
        branch_id=branch.id,
        sale_number="SALE001",
        customer_id=customer.id,
        cashier_id=user.id,
        subtotal=Decimal("250.00"),
        discount_amount=Decimal("0.00"),
        tax_amount=Decimal("0.00"),
        total_amount=Decimal("250.00"),
        payment_method="cash",
        payment_status="completed",
        amount_paid=Decimal("250.00"),
        change_amount=Decimal("0.00"),
        status="completed",
    )
    
    sale_item = SaleItem(
        id=uuid.uuid4(),
        sale_id=sale.id,
        drug_id=drugs[0].id,
            drug_name=drugs[0].name,
        quantity=5,
        batch_id=batch.id,
        unit_price=Decimal("50.00"),
        subtotal=Decimal("250.00"),
        discount_amount=Decimal("0.00"),
        tax_amount=Decimal("0.00"),
        total_price=Decimal("250.00"),
    )
    
    db.add_all([batch, inventory, sale, sale_item])
    await db.commit()
    
    return sale, user
