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
        await shadow.get_changes_since()


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
