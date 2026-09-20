#!/usr/bin/env python3
"""
Bulk E2E seed script — creates ~500 records across all major tables.
Idempotent: skips rows that already exist by primary key.

Usage:
    PYTHONPATH=. python3 scripts/seed_bulk_e2e.py
"""

import asyncio
import uuid
import random
import sys
import os
from decimal import Decimal
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.core.security import hash_password
from app.core.config import get_settings
from app.models.pharmacy.pharmacy_model import Organization, Branch
from app.models.user.user_model import User, Role, UserRole
from app.models.inventory.inventory_model import Drug, DrugCategory
from app.models.inventory.branch_inventory import BranchInventory, DrugBatch
from app.models.sales.sales_model import Sale, SaleItem, Supplier, PurchaseOrder, PurchaseOrderItem
from app.models.customer.customer_model import Customer
from app.models.prescriptions.prescription_model import Prescription

# ── Fixed seed IDs (must match seed_e2e.py) ────────────────────────────────
ORG_ID    = uuid.UUID("11111111-1111-1111-1111-111111111111")
BRANCH_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
ROLE_ID   = uuid.UUID("33333333-3333-3333-3333-333333333333")
USER_ID   = uuid.UUID("44444444-4444-4444-4444-444444444444")
CAT_ID    = uuid.UUID("55555555-5555-5555-5555-555555555555")
PRICE_CONTRACT_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")

DRUG_NAMES = [
    ("Amoxicillin 500mg",   "Amoxicillin",    "capsule",  "500mg",  "prescription", Decimal("15.00"), Decimal("8.00")),
    ("Paracetamol 500mg",   "Acetaminophen",  "tablet",   "500mg",  "otc",          Decimal("5.00"),  Decimal("2.50")),
    ("Ibuprofen 400mg",     "Ibuprofen",      "tablet",   "400mg",  "otc",          Decimal("7.00"),  Decimal("3.50")),
    ("Metformin 500mg",     "Metformin",      "tablet",   "500mg",  "prescription", Decimal("12.00"), Decimal("6.00")),
    ("Amlodipine 5mg",      "Amlodipine",     "tablet",   "5mg",    "prescription", Decimal("18.00"), Decimal("9.00")),
    ("Azithromycin 500mg",  "Azithromycin",   "tablet",   "500mg",  "prescription", Decimal("25.00"), Decimal("14.00")),
    ("Omeprazole 20mg",     "Omeprazole",     "capsule",  "20mg",   "otc",          Decimal("10.00"), Decimal("5.00")),
    ("Ciprofloxacin 500mg", "Ciprofloxacin",  "tablet",   "500mg",  "prescription", Decimal("20.00"), Decimal("11.00")),
    ("Metronidazole 400mg", "Metronidazole",  "tablet",   "400mg",  "prescription", Decimal("8.00"),  Decimal("4.00")),
    ("Vitamin C 500mg",     "Ascorbic Acid",  "tablet",   "500mg",  "otc",          Decimal("4.00"),  Decimal("1.50")),
    ("Diclofenac 50mg",     "Diclofenac",     "tablet",   "50mg",   "otc",          Decimal("6.00"),  Decimal("3.00")),
    ("Folic Acid 5mg",      "Folic Acid",     "tablet",   "5mg",    "otc",          Decimal("3.00"),  Decimal("1.20")),
    ("Losartan 50mg",       "Losartan",       "tablet",   "50mg",   "prescription", Decimal("22.00"), Decimal("12.00")),
    ("Atorvastatin 20mg",   "Atorvastatin",   "tablet",   "20mg",   "prescription", Decimal("30.00"), Decimal("16.00")),
    ("Salbutamol 4mg",      "Salbutamol",     "tablet",   "4mg",    "prescription", Decimal("9.00"),  Decimal("4.50")),
    ("Doxycycline 100mg",   "Doxycycline",    "capsule",  "100mg",  "prescription", Decimal("14.00"), Decimal("7.00")),
    ("Loratadine 10mg",     "Loratadine",     "tablet",   "10mg",   "otc",          Decimal("6.00"),  Decimal("2.80")),
    ("Zinc Sulphate 20mg",  "Zinc Sulphate",  "tablet",   "20mg",   "otc",          Decimal("3.50"),  Decimal("1.40")),
    ("Hydrocortisone 1%",   "Hydrocortisone", "cream",    "1%",     "otc",          Decimal("11.00"), Decimal("5.50")),
    ("Insulin Regular",     "Insulin",        "injection","100IU",  "prescription", Decimal("45.00"), Decimal("28.00")),
]

CUSTOMER_NAMES = [
    ("Kwame", "Mensah"), ("Abena", "Asante"), ("Kofi", "Adu"), ("Ama", "Boateng"),
    ("Yaw", "Ofori"), ("Akua", "Tetteh"), ("Kweku", "Amoah"), ("Adwoa", "Frimpong"),
    ("Kojo", "Owusu"), ("Efua", "Darko"), ("Nana", "Osei"), ("Akosua", "Appiah"),
    ("Fiifi", "Antwi"), ("Maame", "Kumi"), ("Yaa", "Bonsu"), ("Kwabena", "Ampong"),
    ("Esi", "Nyarko"), ("Kwasi", "Sarpong"), ("Adjo", "Quaye"), ("Nii", "Laryea"),
]

SUPPLIER_NAMES = [
    "Accra Medical Supplies", "Kumasi Pharma Wholesale", "Tema Drug Distributors",
    "Cape Coast Meds Ltd", "Takoradi Health Supplies", "Sunyani Pharma Hub",
    "Ho Medical Merchants", "Bolgatanga Drug Store",
]

def new_id() -> uuid.UUID:
    return uuid.uuid4()

def past_date(days_ago_min=1, days_ago_max=365) -> date:
    delta = random.randint(days_ago_min, days_ago_max)
    return (datetime.now(timezone.utc) - timedelta(days=delta)).date()

def future_date(days_ahead_min=30, days_ahead_max=730) -> date:
    delta = random.randint(days_ahead_min, days_ahead_max)
    return (datetime.now(timezone.utc) + timedelta(days=delta)).date()


async def ensure_base_records(db: AsyncSession):
    """Re-run minimal fixed-ID seed so this script can run standalone."""
    from app.core.security import hash_password as _hp

    org = await db.get(Organization, ORG_ID)
    if not org:
        org = Organization(
            id=ORG_ID, name="Demo Pharmacy Org", type="pharmacy",
            is_active=True, subscription_tier="enterprise",
        )
        db.add(org)
        await db.flush()

    branch = await db.get(Branch, BRANCH_ID)
    if not branch:
        branch = Branch(
            id=BRANCH_ID, organization_id=ORG_ID, name="Downtown Main Branch",
            code="DT01", is_active=True,
        )
        db.add(branch)
        await db.flush()

    role = await db.get(Role, ROLE_ID)
    if not role:
        role = Role(
            id=ROLE_ID, organization_id=ORG_ID, name="Admin",
            description="Administrator", level=30, permissions=["*"],
        )
        db.add(role)
        await db.flush()

    user = await db.get(User, USER_ID)
    if not user:
        user = User(
            id=USER_ID, organization_id=ORG_ID,
            assigned_branches=[str(BRANCH_ID)],
            username="admin", email="admin@demopharmacy.com",
            password_hash=_hp("Password123!"),
            full_name="Admin User", is_super_admin=True,
            is_active=True, must_change_password=False,
        )
        db.add(user)
        await db.flush()
        db.add(UserRole(user_id=USER_ID, role_id=ROLE_ID))
        await db.flush()

    cat = await db.get(DrugCategory, CAT_ID)
    if not cat:
        cat = DrugCategory(
            id=CAT_ID, organization_id=ORG_ID, name="General",
            description="General medications",
        )
        db.add(cat)
        await db.flush()

    await db.commit()


async def seed_drugs(db: AsyncSession) -> list[uuid.UUID]:
    """Upsert all 20 drugs and return their IDs."""
    drug_ids = []
    for i, (name, generic, form, strength, dtype, price, cost) in enumerate(DRUG_NAMES):
        sku = f"BULK-{i+1:03d}"
        res = await db.execute(
            select(Drug).where(Drug.organization_id == ORG_ID, Drug.sku == sku)
        )
        drug = res.scalar_one_or_none()
        if not drug:
            drug = Drug(
                id=new_id(), organization_id=ORG_ID, category_id=CAT_ID,
                name=name, generic_name=generic, brand_name=name.split()[0],
                sku=sku, drug_type=dtype, dosage_form=form, strength=strength,
                unit_price=price, cost_price=cost, reorder_level=20, is_active=True,
            )
            db.add(drug)
            await db.flush()
        drug_ids.append(drug.id)
    await db.commit()
    print(f"  ✓ {len(drug_ids)} drugs ready")
    return drug_ids


async def seed_inventory(db: AsyncSession, drug_ids: list[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    """Seed branch_inventory + 2-3 batches per drug. Returns drug_id → batch_id map."""
    drug_to_batch: dict[uuid.UUID, uuid.UUID] = {}
    batch_count = 0
    inv_count = 0

    for i, drug_id in enumerate(drug_ids):
        qty = random.randint(100, 500)
        res = await db.execute(
            select(BranchInventory).where(
                BranchInventory.branch_id == BRANCH_ID,
                BranchInventory.drug_id == drug_id,
            )
        )
        inv = res.scalar_one_or_none()
        if not inv:
            price = DRUG_NAMES[i][5]
            inv = BranchInventory(
                id=new_id(), branch_id=BRANCH_ID, drug_id=drug_id,
                quantity=qty, reserved_quantity=0,
                location=f"Aisle {(i % 5)+1}, Shelf {chr(65 + i % 4)}",
                selling_price=price,
            )
            db.add(inv)
            await db.flush()
            inv_count += 1

        # 2–3 batches
        batches_per_drug = random.randint(2, 3)
        total_remaining = qty
        first_batch_id = None
        for b in range(batches_per_drug):
            batch_qty = (total_remaining // batches_per_drug) + (total_remaining % batches_per_drug if b == 0 else 0)
            batch_num = f"BULK-{i+1:03d}-B{b+1}"
            res2 = await db.execute(
                select(DrugBatch).where(
                    DrugBatch.branch_id == BRANCH_ID,
                    DrugBatch.drug_id == drug_id,
                    DrugBatch.batch_number == batch_num,
                )
            )
            existing = res2.scalar_one_or_none()
            if not existing:
                batch = DrugBatch(
                    id=new_id(), branch_id=BRANCH_ID, drug_id=drug_id,
                    batch_number=batch_num,
                    quantity=batch_qty, remaining_quantity=batch_qty,
                    cost_price=DRUG_NAMES[i][6], selling_price=DRUG_NAMES[i][5],
                    expiry_date=future_date(90, 730),
                )
                db.add(batch)
                await db.flush()
                if first_batch_id is None:
                    first_batch_id = batch.id
                batch_count += 1
            elif first_batch_id is None:
                first_batch_id = existing.id

        if first_batch_id:
            drug_to_batch[drug_id] = first_batch_id

    await db.commit()
    print(f"  ✓ {inv_count} inventory rows + {batch_count} batches created")
    return drug_to_batch


async def seed_customers(db: AsyncSession) -> list[uuid.UUID]:
    customer_ids = []
    created = 0
    for i, (first, last) in enumerate(CUSTOMER_NAMES):
        res = await db.execute(
            select(Customer).where(
                Customer.organization_id == ORG_ID,
                Customer.email == f"{first.lower()}.{last.lower()}@demo.com",
            )
        )
        cust = res.scalar_one_or_none()
        if not cust:
            cust = Customer(
                id=new_id(), organization_id=ORG_ID,
                first_name=first, last_name=last,
                phone=f"024{random.randint(1000000,9999999)}",
                email=f"{first.lower()}.{last.lower()}@demo.com",
                loyalty_points=random.randint(0, 200),
                loyalty_tier=random.choice(["bronze","silver","gold"]),
            )
            db.add(cust)
            await db.flush()
            created += 1
        customer_ids.append(cust.id)
    await db.commit()
    print(f"  ✓ {created} customers created ({len(customer_ids)} total)")
    return customer_ids


async def seed_suppliers(db: AsyncSession) -> list[uuid.UUID]:
    supplier_ids = []
    created = 0
    for name in SUPPLIER_NAMES:
        res = await db.execute(
            select(Supplier).where(Supplier.organization_id == ORG_ID, Supplier.name == name)
        )
        supp = res.scalar_one_or_none()
        if not supp:
            supp = Supplier(
                id=new_id(), organization_id=ORG_ID, name=name,
                contact_person=f"Agent {name[:5]}",
                phone=f"020{random.randint(1000000,9999999)}",
                email=f"info@{name.replace(' ','').lower()[:15]}.com",
                is_active=True, is_deleted=False,
                total_orders=0, total_value=Decimal("0.00"),
            )
            db.add(supp)
            await db.flush()
            created += 1
        supplier_ids.append(supp.id)
    await db.commit()
    print(f"  ✓ {created} suppliers created ({len(supplier_ids)} total)")
    return supplier_ids


async def seed_sales(
    db: AsyncSession,
    drug_ids: list[uuid.UUID],
    drug_to_batch: dict[uuid.UUID, uuid.UUID],
    customer_ids: list[uuid.UUID],
    target: int = 150,
) -> int:
    """Seed ~target completed sales with 1-4 items each."""
    res = await db.execute(
        text("SELECT COUNT(*) FROM sales WHERE branch_id = :b").bindparams(b=BRANCH_ID)
    )
    existing = res.scalar_one()
    if existing >= target:
        print(f"  ✓ {existing} sales already exist — skipping")
        return existing

    to_create = target - int(existing)
    drug_info = {
        drug_ids[i]: {
            "price": DRUG_NAMES[i][5],
            "sku": f"BULK-{i+1:03d}",
            "name": DRUG_NAMES[i][0],
        }
        for i in range(len(drug_ids))
    }
    created = 0

    for n in range(to_create):
        sale_date = past_date(1, 180)
        sale_num = f"DT01-{sale_date.strftime('%Y%m%d')}-{n+1:04d}"
        payment = random.choice(["cash", "card", "mobile_money"])
        n_items = random.randint(1, 4)
        selected_drugs = random.sample(drug_ids, min(n_items, len(drug_ids)))
        cust_id = random.choice(customer_ids) if random.random() > 0.3 else None

        subtotal = Decimal("0.00")
        items_data = []
        for drug_id in selected_drugs:
            qty = random.randint(1, 5)
            unit_price = drug_info[drug_id]["price"]
            item_subtotal = unit_price * qty
            subtotal += item_subtotal
            items_data.append((drug_id, qty, unit_price, item_subtotal,
                               drug_info[drug_id]["name"], drug_info[drug_id]["sku"]))

        sale = Sale(
            id=new_id(), organization_id=ORG_ID, branch_id=BRANCH_ID,
            sale_number=sale_num,
            customer_id=cust_id,
            cashier_id=USER_ID,
            subtotal=subtotal,
            discount_amount=Decimal("0.00"),
            tax_amount=Decimal("0.00"),
            total_amount=subtotal,
            payment_method=payment,
            payment_status="completed",
            status="completed",
            price_contract_id=PRICE_CONTRACT_ID,
            sync_version=1, sync_status="synced",
        )
        db.add(sale)
        await db.flush()

        for drug_id, qty, unit_price, item_subtotal, drug_name, drug_sku in items_data:
            item = SaleItem(
                id=new_id(), sale_id=sale.id,
                drug_id=drug_id,
                drug_name=drug_name,
                drug_sku=drug_sku,
                quantity=qty, refunded_quantity=0,
                unit_price=unit_price,
                subtotal=item_subtotal,
                discount_amount=Decimal("0.00"),
                tax_amount=Decimal("0.00"),
                total_price=item_subtotal,
            )
            db.add(item)

        created += 1
        if created % 25 == 0:
            await db.commit()
            print(f"    ... {created}/{to_create} sales")

    await db.commit()
    print(f"  ✓ {created} sales created")
    return created


async def seed_purchase_orders(
    db: AsyncSession,
    drug_ids: list[uuid.UUID],
    supplier_ids: list[uuid.UUID],
    target: int = 40,
) -> int:
    res = await db.execute(
        text("SELECT COUNT(*) FROM purchase_orders WHERE branch_id = :b").bindparams(b=BRANCH_ID)
    )
    existing = res.scalar_one()
    if existing >= target:
        print(f"  ✓ {existing} purchase orders already exist — skipping")
        return existing

    to_create = target - int(existing)
    created = 0

    for n in range(to_create):
        po_date = past_date(1, 90)
        po_num = f"PO-DT01-{po_date.strftime('%Y%m%d')}-{n+1:04d}"
        status = random.choice(["received", "received", "pending", "ordered"])
        supp_id = random.choice(supplier_ids)
        n_items = random.randint(2, 6)
        selected = random.sample(drug_ids, min(n_items, len(drug_ids)))

        total = Decimal("0.00")
        items_data = []
        for drug_id in selected:
            qty = random.randint(50, 200)
            cost = DRUG_NAMES[drug_ids.index(drug_id)][6]
            items_data.append((drug_id, qty, cost))
            total += cost * qty

        po = PurchaseOrder(
            id=new_id(), organization_id=ORG_ID, branch_id=BRANCH_ID,
            supplier_id=supp_id, po_number=po_num,
            ordered_by=USER_ID,
            status=status, subtotal=total,
            tax_amount=Decimal("0.00"),
            shipping_cost=Decimal("0.00"),
            total_amount=total,
            sync_version=1, sync_status="synced",
        )
        db.add(po)
        await db.flush()

        for drug_id, qty, cost in items_data:
            received = qty if status == "received" else 0
            db.add(PurchaseOrderItem(
                id=new_id(), purchase_order_id=po.id,
                drug_id=drug_id,
                quantity_ordered=qty,
                quantity_received=received,
                unit_cost=cost,
                total_cost=cost * qty,
            ))

        created += 1

    await db.commit()
    print(f"  ✓ {created} purchase orders created")
    return created


async def seed_prescriptions(
    db: AsyncSession,
    customer_ids: list[uuid.UUID],
    drug_ids: list[uuid.UUID],
    target: int = 60,
) -> int:
    res = await db.execute(
        text("SELECT COUNT(*) FROM prescriptions WHERE branch_id = :b").bindparams(b=BRANCH_ID)
    )
    existing = res.scalar_one()
    if existing >= target:
        print(f"  ✓ {existing} prescriptions already exist — skipping")
        return existing

    to_create = target - int(existing)
    created = 0
    rx_drugs = [d for i, d in enumerate(drug_ids) if DRUG_NAMES[i][4] == "prescription"]

    for n in range(to_create):
        rx_num = f"RX-DT01-2026-{n+1:04d}"
        cust_id = random.choice(customer_ids)
        rx_drug_ids = random.sample(rx_drugs, min(random.randint(1, 3), len(rx_drugs)))
        status = random.choice(["filled", "filled", "active", "expired"])

        issue = past_date(1, 180)
        expiry = date(issue.year + 1, issue.month, issue.day)
        med_drug = random.choice(rx_drugs)
        med_idx = drug_ids.index(med_drug)
        medications = [{"drug_id": str(med_drug), "name": DRUG_NAMES[med_idx][0], "dosage": DRUG_NAMES[med_idx][3], "quantity": random.randint(1, 3), "instructions": "Take as directed"}]
        rx = Prescription(
            id=new_id(), organization_id=ORG_ID, branch_id=BRANCH_ID,
            prescription_number=rx_num, customer_id=cust_id,
            prescriber_name=f"Dr. {random.choice(['Kwame','Kofi','Ama','Yaa'])} {random.choice(['Mensah','Asante','Adu'])}",
            prescriber_license=f"GHA-MED-{random.randint(10000,99999)}",
            prescriber_phone=f"030{random.randint(1000000,9999999)}",
            issue_date=issue,
            expiry_date=expiry,
            medications=medications,
            status=status,
            sync_version=1, sync_status="synced",
        )
        db.add(rx)
        created += 1

    await db.commit()
    print(f"  ✓ {created} prescriptions created")
    return created


async def seed_price_contract(db: AsyncSession):
    """Ensure the default price contract exists (required for sales)."""
    # Check by org — there may already be a default contract with a different ID
    res = await db.execute(
        text("SELECT id FROM price_contracts WHERE organization_id = :org AND is_default_contract = TRUE").bindparams(org=ORG_ID)
    )
    row = res.scalar_one_or_none()
    if row is not None:
        global PRICE_CONTRACT_ID
        PRICE_CONTRACT_ID = uuid.UUID(str(row))
        print(f"  ✓ Default price contract already exists: {PRICE_CONTRACT_ID}")
        return
    await db.execute(text("""
        INSERT INTO price_contracts (
            id, organization_id, contract_code, contract_name, contract_type,
            is_default_contract, discount_type, discount_percentage,
            applies_to_prescription_only, applies_to_otc, excluded_drug_categories,
            excluded_drug_ids, applies_to_all_branches, applicable_branch_ids,
            effective_from, requires_verification, allowed_user_roles,
            requires_approval, requires_preauthorization,
            status, is_active, total_transactions, total_discount_given,
            created_by, created_at, updated_at, sync_version, sync_status, is_deleted
        ) VALUES (
            :id, :org, 'STD-001', 'Standard Retail', 'standard',
            TRUE, 'percentage', 0.0,
            FALSE, TRUE, '[]', '[]',
            TRUE, '[]', NOW(),
            FALSE, '["admin","manager","cashier","pharmacist"]', FALSE, FALSE,
            'active', TRUE, 0, 0.0,
            :user, NOW(), NOW(), 1, 'synced', FALSE
        )
    """).bindparams(id=PRICE_CONTRACT_ID, org=ORG_ID, user=USER_ID))
    await db.commit()
    print("  ✓ Default price contract created")


async def main():
    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        print("\n[1/8] Ensuring base records (org / branch / role / user / category)...")
        await ensure_base_records(db)

        print("[2/8] Seeding default price contract...")
        await seed_price_contract(db)

        print("[3/8] Seeding 20 drugs...")
        drug_ids = await seed_drugs(db)

        print("[4/8] Seeding inventory + batches...")
        drug_to_batch = await seed_inventory(db, drug_ids)

        print("[5/8] Seeding 20 customers...")
        customer_ids = await seed_customers(db)

        print("[6/8] Seeding 8 suppliers...")
        supplier_ids = await seed_suppliers(db)

        print("[7/8] Seeding ~150 sales...")
        await seed_sales(db, drug_ids, drug_to_batch, customer_ids, target=150)

        print("[8/8] Seeding 40 purchase orders + 60 prescriptions...")
        await seed_purchase_orders(db, drug_ids, supplier_ids, target=40)
        await seed_prescriptions(db, customer_ids, drug_ids, target=60)

    await engine.dispose()

    # Print totals
    print("\n── Seed summary ──────────────────────────────────")
    print("  20 drugs  •  ~60 batches  •  20 branch_inventory rows")
    print("  20 customers  •  8 suppliers")
    print("  ~150 sales (+ items)  •  40 purchase orders  •  60 prescriptions")
    print("  ≈ 500+ total records")
    print("──────────────────────────────────────────────────\n")


if __name__ == "__main__":
    asyncio.run(main())
