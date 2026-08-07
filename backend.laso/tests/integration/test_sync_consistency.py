"""
Integration tests for verifying data parity between "offline" pushed data and server-side storage.
"""

import os
import pytest
import uuid
from decimal import Decimal
from datetime import date, datetime, timedelta, timezone
from sqlalchemy import select, text
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory.branch_inventory import BranchInventory, DrugBatch
from app.models.pharmacy.pharmacy_model import Branch
from app.models.sales.sales_model import Sale, SaleItem
from app.models.system_md.sys_models import AuditLog, CrrBranchSyncWatermark
from app.schemas.sync_schemas import PushRequest, PushRecord
from app.services.sync.shadow_db import ShadowDB
from app.services.sync.sync_service import SyncService

import main as main_module


@pytest.mark.asyncio
class TestSyncConsistency:
    """Test suite for data consistency after sync."""

    async def test_sale_data_parity(self, db: AsyncSession, setup_test_data):
        """Verify that all relevant fields are correctly persisted after an offline push."""
        org, branch, user, drugs, customer = setup_test_data

        db.add_all([
            BranchInventory(
                branch_id=branch.id, drug_id=drugs[0].id,
                quantity=10, reserved_quantity=0,
            ),
            DrugBatch(
                branch_id=branch.id, drug_id=drugs[0].id,
                batch_number="CONSISTENCY-001-BATCH",
                quantity=10, remaining_quantity=10,
                expiry_date=date.today() + timedelta(days=365),
            ),
        ])
        await db.commit()

        sale_id = uuid.uuid4()

        # Comprehensive sale data matching what the frontend would send
        record_data = {
            "id": str(sale_id),
            "sale_number": "CONSISTENCY-001",
            "customer_id": str(customer.id),
            "customer_name": f"{customer.first_name} {customer.last_name}",
            "subtotal": 150.75,
            "total_discount_amount": 10.25,
            "tax_amount": 5.50,
            "total_amount": 146.00,
            "payment_method": "mobile_money",
            "payment_status": "completed",
            "amount_paid": 150.00,
            "change_amount": 4.00,
            "payment_reference": "TXN-123456",
            "cashier_id": str(user.id),
            "notes": "Offline transaction test",
            "status": "completed",
            "sync_protocol_version": 2,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "items": [
                {
                    "id": str(uuid.uuid4()),
                    "drug_id": str(drugs[0].id),
                    "drug_name": drugs[0].name,
                    "quantity": 3,
                    "unit_price": 50.25,
                    "subtotal": 150.75,
                    "discount_amount": 10.25,
                    "tax_amount": 5.50,
                    "total_price": 146.00,
                }
            ]
        }

        request = PushRequest(
            branch_id=branch.id,
            records=[
                PushRecord(
                    operation_id=uuid.uuid4(),
                    local_id=str(sale_id),
                    table_name="sales",
                    operation="create",
                    sync_version=1,
                    created_offline_at=datetime.now(timezone.utc),
                    data=record_data
                )
            ]
        )

        await SyncService.push(db, request, org.id, user.id)

        # Verify from DB
        result = await db.execute(
            select(Sale)
            .options(selectinload(Sale.items))
            .where(Sale.id == sale_id)
        )
        sale = result.scalar_one()

        assert sale.sale_number == record_data["sale_number"]
        assert float(sale.subtotal) == record_data["subtotal"]
        assert float(sale.discount_amount) == record_data["total_discount_amount"]
        assert float(sale.total_amount) == record_data["total_amount"]
        assert sale.payment_method == record_data["payment_method"]
        assert sale.payment_reference == record_data["payment_reference"]
        assert len(sale.items) == 1
        assert sale.items[0].drug_id == drugs[0].id
        assert float(sale.items[0].unit_price) == record_data["items"][0]["unit_price"]
        assert sale.items[0].quantity == record_data["items"][0]["quantity"]


def _write_branch_inventory_row(conn, row_id: str, branch_id: str) -> None:
    conn.execute(
        "INSERT INTO branch_inventory "
        "(id, branch_id, drug_id, quantity, reserved_quantity, updated_at, created_at) "
        "VALUES (?, ?, 'drug-1', 5, 0, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
        (row_id, branch_id),
    )
    conn.commit()


@pytest.mark.asyncio
class TestCrrCompactionAckBounded:
    """P1-7: ack-bounded retention for CRDT change logs.

    Core safety invariant under test: compaction must never prune a change
    a branch has not yet pulled. Retention is bounded by acknowledgment
    (every ACTIVE branch has confirmed receiving it), never by wall-clock
    age, since a branch can be offline for months and must still be able
    to pull everything it missed on reconnect.
    """

    async def _real_shadow(self, tmp_path):
        shadow = ShadowDB()
        await shadow.initialize(db_path=str(tmp_path / "shadow.db"))
        if not shadow.crr_available:
            pytest.skip("cr-sqlite extension not available in this environment")
        return shadow

    async def test_active_never_synced_branch_blocks_compaction_but_inactive_does_not(
        self, db: AsyncSession, setup_test_data
    ):
        org, branch_synced, user, drugs, customer = setup_test_data

        never_synced_branch = Branch(
            id=uuid.uuid4(), organization_id=org.id, name="Never Synced",
            code="NS01", is_active=True, is_deleted=False,
        )
        closed_branch = Branch(
            id=uuid.uuid4(), organization_id=org.id, name="Closed Branch",
            code="CB01", is_active=False, is_deleted=True,
        )
        db.add_all([never_synced_branch, closed_branch])
        db.add(CrrBranchSyncWatermark(branch_id=branch_synced.id, last_acked_db_version=500))
        # closed_branch: no watermark row at all, but it is inactive/deleted
        # so it must NOT drag the minimum down.
        await db.commit()

        watermark, oldest = await main_module._compute_active_branch_min_watermark(db)

        assert watermark == 0, (
            "never_synced_branch is ACTIVE with no watermark row -- it must "
            "block compaction system-wide (global minimum semantics)"
        )
        assert oldest["branch_id"] == never_synced_branch.id

        # Now the never-synced branch catches up. The closed/inactive branch
        # still has no row at all and must continue to be excluded.
        db.add(CrrBranchSyncWatermark(branch_id=never_synced_branch.id, last_acked_db_version=300))
        await db.commit()

        watermark2, oldest2 = await main_module._compute_active_branch_min_watermark(db)
        assert watermark2 == 300, (
            "minimum across ACTIVE branches only -- the closed branch's total "
            "absence of a watermark row must not affect the computed minimum"
        )
        assert oldest2["branch_id"] == never_synced_branch.id

    async def test_laggard_branch_still_gets_everything_owed_after_compaction(
        self, db: AsyncSession, setup_test_data, tmp_path
    ):
        org, branch_a, user, drugs, customer = setup_test_data
        shadow = await self._real_shadow(tmp_path)
        conn = shadow._conn

        branch_b = Branch(
            id=uuid.uuid4(), organization_id=org.id, name="Branch B",
            code="BB01", is_active=True, is_deleted=False,
        )
        db.add(branch_b)
        await db.commit()

        # History: 3 rows created then deleted (tombstones), plus one row
        # that stays live -- all belonging to branch_a.
        for i in range(3):
            row_id = f"old-{i}"
            _write_branch_inventory_row(conn, row_id, str(branch_a.id))
            conn.execute("DELETE FROM branch_inventory WHERE id = ?", (row_id,))
            conn.commit()
        _write_branch_inventory_row(conn, "still-live", str(branch_a.id))

        version_branch_a_has_seen = await shadow.max_db_version()

        # A NEW change branch_a has not pulled yet.
        _write_branch_inventory_row(conn, "not-yet-pulled", str(branch_a.id))
        latest_version = await shadow.max_db_version()
        assert latest_version > version_branch_a_has_seen

        # Branch B is fully caught up; branch_a (the laggard) only acked
        # through the point BEFORE "not-yet-pulled" was written.
        db.add(CrrBranchSyncWatermark(branch_id=branch_b.id, last_acked_db_version=latest_version))
        db.add(CrrBranchSyncWatermark(branch_id=branch_a.id, last_acked_db_version=version_branch_a_has_seen))
        await db.commit()

        watermark, oldest = await main_module._compute_active_branch_min_watermark(db)
        assert watermark == version_branch_a_has_seen
        assert oldest["branch_id"] == branch_a.id

        deleted = await shadow.compact_crr_tables(watermark)
        assert deleted.get("branch_inventory", 0) >= 1, "at least the old tombstones should be prunable"

        # The laggard now pulls from its own last-acked point and must
        # receive the change it hasn't seen yet, in full -- nothing it was
        # still owed was pruned.
        pull = await shadow.get_changes_since(
            organization_id=str(org.id),
            org_branch_ids=[str(branch_a.id)],
            since_db_version=version_branch_a_has_seen,
        )
        not_yet_pulled_pk = conn.execute(
            "SELECT crsql_pack_columns(id) FROM branch_inventory WHERE id = 'not-yet-pulled'"
        ).fetchone()[0]
        assert not_yet_pulled_pk in {c["pk"] for c in pull}

        # A full pull from zero (a brand-new device on this branch) must
        # still see the row that is still live today.
        full_pull = await shadow.get_changes_since(
            organization_id=str(org.id), org_branch_ids=[str(branch_a.id)], since_db_version=0
        )
        still_live_pk = conn.execute(
            "SELECT crsql_pack_columns(id) FROM branch_inventory WHERE id = 'still-live'"
        ).fetchone()[0]
        assert still_live_pk in {c["pk"] for c in full_pull if c["cid"] != "-1"}

    async def test_audit_logs_pruned_only_once_every_active_branch_has_it(
        self, db: AsyncSession, setup_test_data, tmp_path
    ):
        org, branch, user, drugs, customer = setup_test_data
        shadow = await self._real_shadow(tmp_path)

        db.add(CrrBranchSyncWatermark(branch_id=branch.id, last_acked_db_version=0))
        await db.commit()

        old_log_id = uuid.uuid4()
        new_log_id = uuid.uuid4()
        db.add_all([
            AuditLog(id=old_log_id, organization_id=org.id, action="created_sale"),
            AuditLog(id=new_log_id, organization_id=org.id, action="created_sale"),
        ])
        await db.commit()

        now_iso = datetime.now(timezone.utc).isoformat()
        assert await shadow.upsert_shadow_server_row("audit_logs", {
            "id": str(old_log_id), "organization_id": str(org.id),
            "action": "created_sale", "created_at": now_iso, "updated_at": now_iso,
        })
        old_log_shadow_version = await shadow.get_row_db_version("audit_logs", str(old_log_id))

        assert await shadow.upsert_shadow_server_row("audit_logs", {
            "id": str(new_log_id), "organization_id": str(org.id),
            "action": "created_sale", "created_at": now_iso, "updated_at": now_iso,
        })

        # Watermark sits between the two: everyone has the old log, nobody
        # has necessarily pulled the new one yet.
        watermark = old_log_shadow_version

        deleted = await main_module._compact_postgres_audit_tables(db, shadow, watermark)
        await db.commit()
        assert deleted.get("audit_logs") == 1

        remaining = (await db.execute(select(AuditLog.id))).scalars().all()
        assert old_log_id not in remaining, "acked audit_logs row should be pruned from Postgres"
        assert new_log_id in remaining, "not-yet-acked audit_logs row must survive"

        # The shadow's own copy is also gone (compacted), leaving a
        # tombstone that a later __crsql_clock pass will clean up in turn.
        shadow_row = await shadow.get_merged_row("audit_logs", str(old_log_id))
        assert shadow_row is None

    async def test_crr_renumber_audit_pruned_by_stamped_db_version_not_wall_clock(
        self, db: AsyncSession, setup_test_data
    ):
        """crr_renumber_audit is never pulled by any client, so it has no
        shadow-side db_version of its own -- it is stamped at write time
        with db_version_at_creation (see ShadowDB._log_renumbering) and
        pruned via that stamp, exactly like every other P1-7 retention
        decision: never by age. This exercises the real DDL/INSERT path,
        so it only runs against Postgres (crr_renumber_audit's DDL is
        Postgres-specific — SERIAL/TIMESTAMP WITH TIME ZONE — matching how
        the rest of this file's fixtures already gate Postgres-only
        behaviour off of DATABASE_URL)."""
        if not os.environ.get("DATABASE_URL", "").startswith("postgresql"):
            pytest.skip("crr_renumber_audit DDL is Postgres-only")

        from app.services.sync.shadow_db import _ensure_audit_table, _log_renumbering

        await _ensure_audit_table(db)
        await _log_renumbering(
            db, "sales", "winner-1", "loser-1", "sale_number", "S-1", "S-1-B",
            db_version_at_creation=10,
        )
        await _log_renumbering(
            db, "sales", "winner-2", "loser-2", "sale_number", "S-2", "S-2-B",
            db_version_at_creation=999,
        )
        await db.commit()

        shadow = ShadowDB()  # unused by the postgres-only branch, kept for signature parity
        deleted = await main_module._compact_postgres_audit_tables(db, shadow=_NullShadow(), watermark=50)
        await db.commit()
        assert deleted.get("crr_renumber_audit") == 1

        remaining = await db.execute(text("SELECT event_id FROM crr_renumber_audit"))
        remaining_ids = {row[0] for row in remaining.fetchall()}
        assert "sales:loser-1:S-1:S-1-B" not in remaining_ids
        assert "sales:loser-2:S-2:S-2-B" in remaining_ids


class _NullShadow:
    """Stand-in shadow used only for the crr_renumber_audit-only test above,
    where the audit_logs half of _compact_postgres_audit_tables must not be
    exercised (no rows published) -- get_prunable_row_ids simply returns
    nothing so that branch is a no-op."""

    async def get_prunable_row_ids(self, table, watermark, limit=500):
        return []
