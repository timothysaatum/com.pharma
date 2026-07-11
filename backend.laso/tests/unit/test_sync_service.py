import uuid
import sqlite3
from datetime import datetime
from decimal import Decimal

import pytest

from app.api.v1.endpoints.sync_endpoints import _user_can_sync_branch
from app.schemas.sync_schemas import CrrPushRecord, PullRequest, PullResponse
from app.services.sync.sync_service import SyncService
from app.services.sync.shadow_db import ShadowDB, _CRR_TABLE_CONFIG


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
    from app.services.sync.shadow_db import _resolve_extension_path

    monkeypatch.delenv("CRSQLITE_EXTENSION_PATH", raising=False)
    monkeypatch.chdir(tmp_path)

    resolved = _resolve_extension_path()
    assert resolved is not None
    assert resolved.endswith("ui.laso/src-tauri/crsqlite.so")


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
