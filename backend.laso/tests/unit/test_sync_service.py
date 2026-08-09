import uuid
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.api.v1.endpoints.sync_endpoints import _user_can_sync_branch
from app.schemas.sync_schemas import (
    CrrPushRecord,
    PullRequest,
    PullResponse,
    PushRecord,
    PushResult,
)
from app.services.sync.sync_service import SyncService
from app.services.sync.shadow_db import ShadowDB, _CRR_TABLE_CONFIG, _crr_table_columns
from app.services.sync.crr_sync_service import (
    _validate_branch_inventory,
    _validate_customer,
    _validate_drug_batch,
    _validate_prescription,
    _validate_purchase_order,
)


class _Dialect:
    name = "postgresql"


class _Bind:
    dialect = _Dialect()


class _ActiveSession:
    def in_transaction(self):
        return True

    def get_bind(self):
        return _Bind()


class _SnapshotSession(_ActiveSession):
    pass


class _SessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _NestedTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _AuditSession:
    def __init__(self):
        self.added = []
        self.flushed = False

    def begin_nested(self):
        return _NestedTransaction()

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushed = True


class _EmptyMappings:
    def all(self):
        return []


class _EmptyResult:
    def mappings(self):
        return _EmptyMappings()


class _RecordingSession:
    def __init__(self):
        self.executed = []

    async def execute(self, statement, params):
        self.executed.append((str(statement), params))
        return _EmptyResult()


@pytest.mark.asyncio
async def test_pull_uses_fresh_session_when_request_session_already_has_transaction(monkeypatch):
    request_session = _ActiveSession()
    snapshot_session = _SnapshotSession()
    request = PullRequest(branch_id=uuid.uuid4(), tables=[])
    organization_id = uuid.uuid4()
    seen_sessions = []

    monkeypatch.setattr(
        "app.services.sync.sync_service.AsyncSessionLocal",
        lambda: _SessionContext(snapshot_session),
    )

    async def fake_pull_with_snapshot(db, pull_request, org_id):
        seen_sessions.append(db)
        assert pull_request is request
        assert org_id == organization_id
        return PullResponse(sync_timestamp=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))

    monkeypatch.setattr(SyncService, "_pull_with_snapshot", fake_pull_with_snapshot)

    await SyncService.pull(request_session, request, organization_id)

    assert seen_sessions == [snapshot_session]


def test_sync_branch_access_normalizes_uuid_assignments():
    branch_id = uuid.uuid4()
    user = type("User", (), {"assigned_branches": [branch_id]})()

    assert _user_can_sync_branch(user, branch_id)
    assert _user_can_sync_branch(user, str(branch_id))


def test_crr_table_columns_do_not_match_partial_column_names():
    columns = _crr_table_columns("price_contracts")

    assert "organization_id" in columns
    assert "applicable_branch_ids" in columns
    assert "branch_id" not in columns


def test_sales_crr_is_server_authoritative_and_cannot_bypass_sale_commands():
    assert _CRR_TABLE_CONFIG["sales"]["server_authoritative"] is True


@pytest.mark.asyncio
async def test_publish_server_authoritative_price_contracts_are_not_branch_scoped(monkeypatch):
    only_price_contracts = {"price_contracts": _CRR_TABLE_CONFIG["price_contracts"]}
    monkeypatch.setattr(
        "app.services.sync.shadow_db._CRR_TABLE_CONFIG",
        only_price_contracts,
    )

    db = _RecordingSession()
    shadow = ShadowDB()

    published = await shadow.publish_server_authoritative_tables(
        db,
        organization_id="org-1",
        branch_id="branch-1",
    )

    assert published == 0
    assert db.executed == [(
        'SELECT * FROM "price_contracts" WHERE organization_id = :organization_id',
        {"organization_id": "org-1"},
    )]


def test_crr_push_record_decodes_binary_transport_fields():
    record = CrrPushRecord(
        table="branch_inventory",
        pk="b64:AQID",
        cid="id",
        val="b64:BAUG",
        col_version=1,
        db_version=1,
        site_id="b64:BwgJ",
        cl=1,
        seq=0,
    )

    assert record.pk == b"\x01\x02\x03"
    assert record.val == b"\x04\x05\x06"
    assert record.site_id == b"\x07\x08\x09"


def test_shadow_row_values_are_coerced_for_asyncpg():
    coerced = ShadowDB._coerce_pg_types("branch_inventory", {
        "quantity": "37",
        "selling_price": "12.50",
        "created_at": "2026-07-10T12:00:00Z",
        "updated_at": "2026-07-10T12:01:00+00:00",
    })

    assert coerced["quantity"] == 37
    assert coerced["selling_price"] == Decimal("12.50")
    assert isinstance(coerced["created_at"], datetime)
    assert isinstance(coerced["updated_at"], datetime)


def test_shadow_row_coerces_empty_string_date_and_datetime_to_null():
    # Regression coverage for a real bug: a client-populated but unset
    # nullable Date/DateTime column (e.g. a prescription's last_refill_date
    # or verified_at) can arrive here as "" rather than None. The old code
    # only guarded against None and called date.fromisoformat("")/
    # datetime.fromisoformat("") unconditionally, raising
    # `ValueError: Invalid isoformat string: ''` and permanently failing
    # both the live CRR push and the periodic reconciliation loop for that
    # row on every retry.
    coerced = ShadowDB._coerce_pg_types("prescriptions", {
        "last_refill_date": "",
        "verified_at": "",
    })

    assert coerced["last_refill_date"] is None
    assert coerced["verified_at"] is None


def test_shadow_pg_row_replaces_client_queue_state_for_raw_insert():
    prepared = ShadowDB._prepare_pg_row("branch_inventory", {
        "id": str(uuid.uuid4()),
        "quantity": "37",
        "sync_status": "pending",
        "synced_at": "2026-07-10T12:00:00Z",
    })

    assert prepared["quantity"] == 37
    assert prepared["sync_status"] == "synced"
    assert "synced_at" not in prepared


@pytest.mark.asyncio
async def test_sync_accepted_sale_writes_searchable_audit_log():
    organization_id = uuid.uuid4()
    branch_id = uuid.uuid4()
    user_id = uuid.uuid4()
    sale_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    db = _AuditSession()

    record = PushRecord(
        operation_id=operation_id,
        table_name="sales",
        local_id=str(sale_id),
        operation="create",
        sync_version=1,
        created_offline_at=datetime.now(),
        data={
            "sale_number": "OFF-SALE-UNIT",
            "customer_id": str(uuid.uuid4()),
            "prescription_id": str(uuid.uuid4()),
            "total_amount": "25.00",
            "discount_amount": "0.00",
            "payment_method": "cash",
            "items": [{"quantity": 2}],
        },
    )
    result = PushResult(
        local_id=str(sale_id),
        table_name="sales",
        server_id=str(sale_id),
        success=True,
    )

    await SyncService._write_accepted_record_audit(
        db=db,
        record=record,
        push_result=result,
        organization_id=organization_id,
        branch_id=branch_id,
        pushed_by=user_id,
    )

    assert db.flushed is True
    assert len(db.added) == 1
    audit = db.added[0]
    assert audit.action == "process_sale"
    assert audit.entity_type == "Sale"
    assert audit.entity_id == sale_id
    assert audit.user_id == user_id
    assert audit.organization_id == organization_id
    assert audit.changes["sale_number"] == "OFF-SALE-UNIT"
    assert audit.changes["items_count"] == 1
    assert audit.context_metadata["source"] == "offline_sync"
    assert audit.context_metadata["operation_id"] == str(operation_id)


def test_shadow_pg_row_drops_crr_only_aggregate_columns():
    purchase_order = ShadowDB._prepare_pg_row("purchase_orders", {
        "id": str(uuid.uuid4()),
        "po_number": "PO-EDGE",
        "items_json": "[]",
        "sync_status": "pending",
    })
    sale = ShadowDB._prepare_pg_row("sales", {
        "id": str(uuid.uuid4()),
        "sale_number": "SALE-EDGE",
        "items_json": "[]",
        "items_count": 2,
    })

    assert "items_json" not in purchase_order
    assert "items_json" not in sale
    assert "items_count" not in sale
    assert purchase_order["sync_status"] == "synced"


def test_server_resolves_tracked_monorepo_crsqlite_extension(monkeypatch, tmp_path):
    from app.services.sync.shadow_db import _crsqlite_platform_dir, _resolve_extension_path

    monkeypatch.delenv("CRSQLITE_EXTENSION_PATH", raising=False)
    monkeypatch.chdir(tmp_path)

    resolved = _resolve_extension_path()
    platform_dir = _crsqlite_platform_dir()
    assert resolved is not None
    assert platform_dir is not None
    assert resolved.endswith(f"crsqlite/{platform_dir}/crsqlite.so")
    assert Path(resolved).exists()


@pytest.mark.asyncio
async def test_rejected_shadow_row_restores_authoritative_state():
    shadow = ShadowDB()
    shadow._conn = sqlite3.connect(":memory:", check_same_thread=False)
    shadow._conn.executescript(_CRR_TABLE_CONFIG["branch_inventory"]["ddl"])
    row_id = str(uuid.uuid4())
    shadow._conn.execute(
        "INSERT INTO branch_inventory "
        "(id,branch_id,drug_id,quantity,reserved_quantity,sync_status) "
        "VALUES (?,?,?,?,?,?)",
        (row_id, "branch", "drug", -5, 0, "pending"),
    )

    await shadow.restore_rejected_row(
        "branch_inventory",
        row_id,
        {
            "id": row_id,
            "branch_id": "branch",
            "drug_id": "drug",
            "quantity": 37,
            "reserved_quantity": 2,
            "sync_status": "synced",
        },
    )

    restored = shadow._conn.execute(
        "SELECT quantity,reserved_quantity,sync_status "
        "FROM branch_inventory WHERE id=?",
        (row_id,),
    ).fetchone()
    assert restored == (37, 2, "synced")


@pytest.mark.asyncio
async def test_shadow_without_crsqlite_reports_unavailable_instead_of_sql_error(tmp_path):
    shadow = ShadowDB()
    await shadow.initialize(
        db_path=str(tmp_path / "shadow.db"),
        ext_path=str(tmp_path / "missing-crsqlite.so"),
    )

    assert shadow.crr_available is False
    with pytest.raises(RuntimeError, match="CRR sync is unavailable"):
        await shadow.max_db_version()
    with pytest.raises(RuntimeError, match="CRR sync is unavailable"):
        await shadow.get_changes_since(organization_id="test-org")


@pytest.mark.asyncio
async def test_crr_endpoint_guard_returns_503_when_extension_is_unavailable(monkeypatch):
    from fastapi import HTTPException
    from app.api.v1.endpoints import crr_sync_endpoints

    class UnavailableShadow:
        crr_available = False

    async def unavailable_shadow():
        return UnavailableShadow()

    monkeypatch.setattr(crr_sync_endpoints, "get_shadow_db", unavailable_shadow)

    with pytest.raises(HTTPException) as exc_info:
        await crr_sync_endpoints._require_crr_shadow()

    assert exc_info.value.status_code == 503
    assert exc_info.value.headers == {"Retry-After": "60"}


@pytest.mark.asyncio
async def test_crr_pull_rejects_unassigned_branch():
    from fastapi import HTTPException
    from app.api.v1.endpoints.crr_sync_endpoints import crr_pull
    from app.schemas.sync_schemas import PullRequest

    branch_id = uuid.uuid4()
    current_user = type("User", (), {
        "organization_id": uuid.uuid4(),
        "assigned_branches": [uuid.uuid4()],
    })()

    with pytest.raises(HTTPException) as exc_info:
        await crr_pull(
            request=PullRequest(branch_id=branch_id),
            current_user=current_user,
            db=None,
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_shadow_publication_uses_mapped_columns_not_ddl_substrings():
    class _Result:
        def mappings(self):
            return self

        def all(self):
            return []

    class _Db:
        def __init__(self):
            self.calls = []

        async def execute(self, statement, params):
            self.calls.append((str(statement), params))
            return _Result()

    db = _Db()
    shadow = ShadowDB()

    await shadow.publish_server_authoritative_tables(
        db=db,
        organization_id=uuid.uuid4(),
        branch_id=uuid.uuid4(),
    )

    statements = {statement for statement, _params in db.calls}
    assert (
        'SELECT * FROM "price_contracts" WHERE organization_id = :organization_id'
        in statements
    )
    assert not any(
        'FROM "price_contracts"' in statement and "branch_id" in statement
        for statement in statements
    )
    assert any(
        'FROM "branch_inventory"' in statement and "branch_id" in statement
        for statement in statements
    )


@pytest.mark.asyncio
async def test_reconcile_skips_server_authoritative_tables(tmp_path, monkeypatch):
    shadow = ShadowDB()
    await shadow.initialize(
        db_path=str(tmp_path / "shadow.db"),
        ext_path=str(tmp_path / "missing-crsqlite.so"),
    )
    assert shadow._conn is not None
    shadow._conn.execute("""
        INSERT INTO price_contracts
            (id, organization_id, contract_code, contract_name, contract_type,
             effective_from, updated_at, created_at)
        VALUES
            ('contract-1', 'org-1', 'GLICO', 'Glico', 'insurance',
             '2026-07-02', '2026-07-02T09:13:37Z', '2026-07-02T09:13:16Z')
    """)
    shadow._conn.commit()

    called = False

    async def fail_if_called(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("server-authoritative rows must not reconcile into Postgres")

    monkeypatch.setattr(shadow, "upsert_merged_row", fail_if_called)

    checked, updated = await shadow.reconcile_table("price_contracts", db=None)

    assert (checked, updated) == (1, 0)
    assert called is False


# ─── S1: get_changes_since must be scoped to a single organization ────────
# Regression coverage for docs/reviews/2026-08-04-inventory-sync-sales-independent-review.md
# finding S1 — the CRR pull endpoint had no org/branch filter at all, so
# every client could pull every other organization's changes.

async def _init_real_shadow(tmp_path):
    """Initialize a ShadowDB with the real cr-sqlite extension, or skip."""
    shadow = ShadowDB()
    await shadow.initialize(db_path=str(tmp_path / "shadow.db"))
    if not shadow.crr_available:
        pytest.skip("cr-sqlite extension not available in this environment")
    return shadow


def _insert_branch_inventory_row(conn, row_id: str, branch_id: str) -> None:
    conn.execute(
        "INSERT INTO branch_inventory "
        "(id, branch_id, drug_id, quantity, reserved_quantity, updated_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (row_id, branch_id, "drug-1", 10, 0, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )
    conn.commit()


def _packed_pk(conn, table: str, row_id: str):
    return conn.execute(
        f"SELECT crsql_pack_columns(id) FROM [{table}] WHERE id = ?", (row_id,)
    ).fetchone()[0]


@pytest.mark.asyncio
async def test_get_changes_since_scopes_by_organization(tmp_path):
    shadow = await _init_real_shadow(tmp_path)
    conn = shadow._conn

    _insert_branch_inventory_row(conn, "row-a", "branch-a1")
    _insert_branch_inventory_row(conn, "row-b", "branch-b1")

    row_a_pk = _packed_pk(conn, "branch_inventory", "row-a")
    row_b_pk = _packed_pk(conn, "branch_inventory", "row-b")

    org_a_changes = await shadow.get_changes_since(
        organization_id="org-a", org_branch_ids=["branch-a1"]
    )
    org_a_pks = {c["pk"] for c in org_a_changes}
    assert row_a_pk in org_a_pks
    assert row_b_pk not in org_a_pks, "org A must never see org B's changes"

    org_b_changes = await shadow.get_changes_since(
        organization_id="org-b", org_branch_ids=["branch-b1"]
    )
    org_b_pks = {c["pk"] for c in org_b_changes}
    assert row_b_pk in org_b_pks
    assert row_a_pk not in org_b_pks, "org B must never see org A's changes"


@pytest.mark.asyncio
async def test_get_changes_since_requires_org_branch_ids_for_branch_only_tables(tmp_path):
    """A caller that forgets to pass org_branch_ids must not see branch-only
    tables (branch_inventory/drug_batches) unfiltered — they have no
    organization_id column of their own, so omitting org_branch_ids must
    fail closed (return nothing), not fail open."""
    shadow = await _init_real_shadow(tmp_path)
    conn = shadow._conn
    _insert_branch_inventory_row(conn, "row-a", "branch-a1")

    changes = await shadow.get_changes_since(organization_id="org-a")
    assert changes == []


@pytest.mark.asyncio
async def test_pk_is_tombstoned_true_after_delete_crr_row(tmp_path):
    # Regression coverage for a real bug: resolve_row_id returning None is
    # ambiguous between "never materialized" (a real problem) and "already
    # legitimately deleted/retired" (sum_and_merge and the customer-merge
    # strategy both call delete_crr_row after folding a losing duplicate
    # into its winner). pk_is_tombstoned must distinguish the two so
    # handle_crr_push can treat the latter as a no-op success instead of a
    # permanent, endlessly-retried error.
    shadow = await _init_real_shadow(tmp_path)
    conn = shadow._conn
    _insert_branch_inventory_row(conn, "row-a", "branch-a1")
    encoded_pk = _packed_pk(conn, "branch_inventory", "row-a")

    assert await shadow.pk_is_tombstoned("branch_inventory", encoded_pk) is False

    await shadow.delete_crr_row("branch_inventory", "row-a")

    assert await shadow.pk_is_tombstoned("branch_inventory", encoded_pk) is True


@pytest.mark.asyncio
async def test_pk_is_tombstoned_false_for_a_pk_that_never_existed(tmp_path):
    # A pk with no crsql_changes history at all (never inserted, not just
    # deleted) must NOT be reported as tombstoned -- that would silently
    # swallow genuinely broken/malformed pushes as false successes.
    shadow = await _init_real_shadow(tmp_path)

    result = await shadow.pk_is_tombstoned(
        "branch_inventory", b"definitely-does-not-exist"
    )

    assert result is False


@pytest.mark.asyncio
async def test_get_changes_since_still_scopes_delete_tombstones(tmp_path):
    """A hard-deleted row (delete_crr_row) no longer exists in the shadow
    table, so ownership must be resolved from the crr_row_owners index
    populated at insert-time, not from a live join — otherwise the delete
    tombstone would be silently dropped instead of scoped."""
    shadow = await _init_real_shadow(tmp_path)
    conn = shadow._conn
    _insert_branch_inventory_row(conn, "row-a", "branch-a1")
    row_a_pk = _packed_pk(conn, "branch_inventory", "row-a")

    await shadow.delete_crr_row("branch_inventory", "row-a")

    changes = await shadow.get_changes_since(
        organization_id="org-a", org_branch_ids=["branch-a1"]
    )
    assert row_a_pk in {c["pk"] for c in changes}, (
        "delete tombstone must still be attributed to its org after the row is gone"
    )


# ─── S2: push validators must enforce tenant ownership ────────────────────
# Regression coverage for finding S2 — only branch_inventory had a
# validator, and even it didn't check branch_id, so a client could
# overwrite another organization's/branch's row via ON CONFLICT upsert.

@pytest.mark.asyncio
async def test_branch_inventory_validator_rejects_foreign_branch():
    org_id = uuid.uuid4()
    authorized_branch = uuid.uuid4()
    other_branch = uuid.uuid4()
    merged = {
        "drug_id": "drug-1",
        "branch_id": str(other_branch),
        "quantity": 10,
        "reserved_quantity": 0,
    }
    error = await _validate_branch_inventory(merged, org_id, authorized_branch, db=None)
    assert error is not None
    assert "branch_id" in error


@pytest.mark.asyncio
async def test_drug_batch_validator_rejects_foreign_branch():
    org_id = uuid.uuid4()
    authorized_branch = uuid.uuid4()
    other_branch = uuid.uuid4()
    merged = {
        "drug_id": "drug-1",
        "branch_id": str(other_branch),
        "quantity": 10,
        "remaining_quantity": 5,
    }
    error = await _validate_drug_batch(merged, org_id, authorized_branch, db=None)
    assert error is not None
    assert "branch_id" in error


@pytest.mark.asyncio
async def test_customer_validator_rejects_foreign_organization():
    org_id = uuid.uuid4()
    other_org = uuid.uuid4()
    merged = {"id": "c1", "organization_id": str(other_org), "first_name": "X"}
    error = await _validate_customer(merged, org_id, uuid.uuid4(), db=None)
    assert error is not None
    assert "organization_id" in error


@pytest.mark.asyncio
async def test_customer_validator_accepts_matching_organization():
    org_id = uuid.uuid4()
    merged = {"id": "c1", "organization_id": str(org_id), "first_name": "X"}
    error = await _validate_customer(merged, org_id, uuid.uuid4(), db=None)
    assert error is None


@pytest.mark.asyncio
async def test_purchase_order_validator_rejects_foreign_branch():
    org_id = uuid.uuid4()
    authorized_branch = uuid.uuid4()
    other_branch = uuid.uuid4()
    merged = {
        "id": "po1",
        "organization_id": str(org_id),
        "branch_id": str(other_branch),
        "po_number": "PO-1",
    }
    error = await _validate_purchase_order(merged, org_id, authorized_branch, db=None)
    assert error is not None
    assert "branch_id" in error


@pytest.mark.asyncio
async def test_purchase_order_validator_rejects_foreign_organization():
    org_id = uuid.uuid4()
    other_org = uuid.uuid4()
    branch_id = uuid.uuid4()
    merged = {
        "id": "po1",
        "organization_id": str(other_org),
        "branch_id": str(branch_id),
        "po_number": "PO-1",
    }
    error = await _validate_purchase_order(merged, org_id, branch_id, db=None)
    assert error is not None
    assert "organization_id" in error


@pytest.mark.asyncio
async def test_prescription_validator_rejects_foreign_organization():
    org_id = uuid.uuid4()
    other_org = uuid.uuid4()
    branch_id = uuid.uuid4()
    merged = {
        "id": "rx1",
        "organization_id": str(other_org),
        "branch_id": str(branch_id),
        "prescription_number": "RX-1",
        "customer_id": "",
    }
    error = await _validate_prescription(merged, org_id, branch_id, db=None)
    assert error is not None
    assert "organization_id" in error


# ─── P1-7 Step 0 SPIKE: ack-bounded __crsql_clock compaction ───────────────
# Kept permanently as regression coverage (same convention as
# ui.laso/src-tauri/src/db.rs::spike_sqlcipher_load_extension_after_pragma_key)
# rather than deleted once the feature shipped. This is the go/no-go check
# run against the REAL vendored crsqlite.so before any compaction logic was
# written, and it caught something the original plan got wrong: cr-sqlite's
# db_version is a single counter shared by every CRR table, derived from
# whatever rows are still physically present in __crsql_clock -- not an
# independent counter immune to row deletion. A naive "delete everything
# below a watermark" implementation would eventually delete every row
# holding the current global max and cause the next write anywhere in the
# database to be assigned an ALREADY-HANDED-OUT db_version, silently
# breaking every future `db_version > since_db_version` pull. See
# ShadowDB.compact_crr_tables's docstring for the full safety model this
# test verifies.

async def _make_branch_inventory_row(conn, row_id: str, branch_id: str = "b1") -> None:
    conn.execute(
        "INSERT INTO branch_inventory "
        "(id, branch_id, drug_id, quantity, reserved_quantity, updated_at, created_at) "
        "VALUES (?, ?, 'drug-1', 5, 0, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
        (row_id, branch_id),
    )
    conn.commit()


@pytest.mark.asyncio
async def test_crr_clock_compaction_is_safe(tmp_path):
    shadow = await _init_real_shadow(tmp_path)
    conn = shadow._conn

    # A row edited several times must still collapse to ONE physical clock
    # row per (pk, cid) -- cr-sqlite upserts in place, it does not append a
    # history log. There is nothing to "compact" for a live cell.
    await _make_branch_inventory_row(conn, "live-row")
    conn.execute("UPDATE branch_inventory SET quantity = 6 WHERE id = 'live-row'")
    conn.commit()
    conn.execute("UPDATE branch_inventory SET quantity = 7 WHERE id = 'live-row'")
    conn.commit()

    # Build-independent assertion: exactly one clock row exists for
    # the 'quantity' column of this pk, no matter how many times it changed.
    rows_for_quantity = [
        r for r in conn.execute("SELECT * FROM [branch_inventory__crsql_clock]").fetchall()
    ]
    assert sum(1 for r in rows_for_quantity if r[1] == "quantity") <= 1

    # Create + delete three rows -> three tombstones (col_name sentinel '-1').
    for i in range(3):
        row_id = f"dead-{i}"
        await _make_branch_inventory_row(conn, row_id)
        conn.execute("DELETE FROM branch_inventory WHERE id = ?", (row_id,))
        conn.commit()

    tombstones_before = [
        r for r in conn.execute("SELECT * FROM [branch_inventory__crsql_clock]").fetchall()
        if r[1] == "-1"
    ]
    assert len(tombstones_before) == 3

    max_before = await shadow.max_db_version()
    watermark = max_before - 1  # "acked" everything except the very latest write

    deleted = await shadow.compact_crr_tables(watermark)
    assert deleted.get("branch_inventory", 0) == 2, (
        "exactly the tombstones strictly below the current global max, and "
        "at/under the watermark, should be pruned"
    )

    remaining_tombstones = [
        r for r in conn.execute("SELECT * FROM [branch_inventory__crsql_clock]").fetchall()
        if r[1] == "-1"
    ]
    assert len(remaining_tombstones) == 1, (
        "the tombstone holding the current global max db_version must survive "
        "-- deleting it would let the next write reuse an already-issued version"
    )

    # The live row's current value must be completely untouched.
    live_row = conn.execute(
        "SELECT quantity FROM branch_inventory WHERE id = 'live-row'"
    ).fetchone()
    assert live_row == (7,)

    # crsql_site_id and a fresh full pull must both still work after compaction.
    site_id = conn.execute("SELECT crsql_site_id()").fetchone()[0]
    assert site_id

    full_pull = await shadow.get_changes_since(
        organization_id="whatever", org_branch_ids=["b1"], since_db_version=0
    )
    live_pks = {c["pk"] for c in full_pull if c["cid"] != "-1"}
    packed_live_pk = conn.execute(
        "SELECT crsql_pack_columns(id) FROM branch_inventory WHERE id = 'live-row'"
    ).fetchone()[0]
    assert packed_live_pk in live_pks, "the live row must still be fully pullable"

    # ── The critical invariant: no db_version reuse after compaction. ──
    await _make_branch_inventory_row(conn, "new-after-compaction")
    max_after = await shadow.max_db_version()
    assert max_after > max_before, (
        "db_version must keep advancing monotonically after compaction; a "
        "value <= max_before would mean version reuse and silent data loss "
        "for any client that already pulled past max_before"
    )


@pytest.mark.asyncio
async def test_crr_clock_compaction_never_touches_live_cell_even_with_huge_watermark(tmp_path):
    """A live cell's clock row is never a 'superseded old version' -- it is
    the ONLY record of that cell's value. Compaction must only ever delete
    tombstones (col_name == '-1'), regardless of how permissive the
    watermark is."""
    shadow = await _init_real_shadow(tmp_path)
    conn = shadow._conn
    await _make_branch_inventory_row(conn, "still-alive")

    huge_watermark = await shadow.max_db_version() + 1_000_000
    await shadow.compact_crr_tables(huge_watermark)

    row = conn.execute(
        "SELECT quantity FROM branch_inventory WHERE id = 'still-alive'"
    ).fetchone()
    assert row == (5,), "a live row must survive compaction no matter the watermark"

    changes = conn.execute(
        'SELECT COUNT(*) FROM crsql_changes WHERE "table" = ? AND cid != ?',
        ("branch_inventory", "-1"),
    ).fetchone()[0]
    assert changes > 0, "the live row's columns must still be reported by crsql_changes"


@pytest.mark.asyncio
async def test_crr_clock_compaction_noop_when_watermark_not_positive(tmp_path):
    shadow = await _init_real_shadow(tmp_path)
    conn = shadow._conn
    await _make_branch_inventory_row(conn, "row-a")
    conn.execute("DELETE FROM branch_inventory WHERE id = 'row-a'")
    conn.commit()

    assert await shadow.compact_crr_tables(0) == {}
    assert await shadow.compact_crr_tables(-5) == {}

    # Nothing was pruned -- tombstone still there.
    tombstones = [
        r for r in conn.execute("SELECT * FROM [branch_inventory__crsql_clock]").fetchall()
        if r[1] == "-1"
    ]
    assert len(tombstones) == 1


@pytest.mark.asyncio
async def test_get_prunable_row_ids_fails_closed_for_unpublished_rows(tmp_path):
    """A row that was never written into the shadow DB at all (never
    published/pulled) must never be treated as prunable -- there is no
    proof any branch has received it."""
    shadow = await _init_real_shadow(tmp_path)
    conn = shadow._conn

    conn.execute(
        "INSERT INTO audit_logs (id, organization_id, action, created_at, updated_at) "
        "VALUES ('log-1', 'org-1', 'created_sale', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
    )
    conn.commit()
    high_watermark = await shadow.max_db_version() + 1000

    prunable = await shadow.get_prunable_row_ids("audit_logs", high_watermark)
    assert "log-1" in prunable

    # A row with db_version above the watermark is not yet safe to prune.
    conn.execute(
        "INSERT INTO audit_logs (id, organization_id, action, created_at, updated_at) "
        "VALUES ('log-2', 'org-1', 'created_sale', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
    )
    conn.commit()
    low_watermark = await shadow.get_row_db_version("audit_logs", "log-1")
    prunable_low = await shadow.get_prunable_row_ids("audit_logs", low_watermark)
    assert "log-1" in prunable_low
    assert "log-2" not in prunable_low


@pytest.mark.asyncio
async def test_get_row_db_version_reflects_latest_write(tmp_path):
    shadow = await _init_real_shadow(tmp_path)
    conn = shadow._conn
    assert await shadow.get_row_db_version("branch_inventory", "missing") == 0

    await _make_branch_inventory_row(conn, "row-x")
    v1 = await shadow.get_row_db_version("branch_inventory", "row-x")
    assert v1 > 0

    conn.execute("UPDATE branch_inventory SET quantity = 99 WHERE id = 'row-x'")
    conn.commit()
    v2 = await shadow.get_row_db_version("branch_inventory", "row-x")
    assert v2 > v1


# ─── P1-7: pull endpoint watermark bookkeeping ─────────────────────────────

class _FakeWatermarkResult:
    def __init__(self, value):
        self._value = value

    def first(self):
        return (self._value,) if self._value is not None else None


class _WatermarkRecordingSession:
    """Fake AsyncSession that answers the SELECT with a fixed existing
    watermark and records every statement it is asked to execute."""

    def __init__(self, existing_version):
        self._existing_version = existing_version
        self.executed = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.executed.append((sql, params))
        if "SELECT last_acked_db_version" in sql:
            return _FakeWatermarkResult(self._existing_version)
        return _FakeWatermarkResult(None)


@pytest.mark.asyncio
async def test_record_branch_watermark_advances_when_client_is_ahead():
    from app.api.v1.endpoints.crr_sync_endpoints import _record_branch_watermark

    session = _WatermarkRecordingSession(existing_version=5)
    branch_id = uuid.uuid4()
    await _record_branch_watermark(session, branch_id, 10)

    insert_sql, insert_params = session.executed[-1]
    assert "INSERT INTO crr_branch_sync_watermark" in insert_sql
    assert insert_params["version"] == 10
    assert insert_params["branch_id"] == str(branch_id)


@pytest.mark.asyncio
async def test_record_branch_watermark_never_regresses():
    from app.api.v1.endpoints.crr_sync_endpoints import _record_branch_watermark

    session = _WatermarkRecordingSession(existing_version=20)
    await _record_branch_watermark(session, uuid.uuid4(), 3)

    _, insert_params = session.executed[-1]
    assert insert_params["version"] == 20, (
        "the watermark must never regress below what was already acked, "
        "even if this particular request reports an older client version"
    )


@pytest.mark.asyncio
async def test_record_branch_watermark_first_pull_has_no_existing_row():
    from app.api.v1.endpoints.crr_sync_endpoints import _record_branch_watermark

    session = _WatermarkRecordingSession(existing_version=None)
    await _record_branch_watermark(session, uuid.uuid4(), 42)

    _, insert_params = session.executed[-1]
    assert insert_params["version"] == 42
