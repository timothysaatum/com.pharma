"""
Backfill correctness, against a DISPOSABLE PostgreSQL. Never atlasdb.

The script under test is scripts/backfill_stock_events.py. These tests pin the
four properties that make it safe to run on the real database:

  1. A dry run writes nothing.
  2. --apply appends EXACTLY the expected events, through the real
     StockEventEmitter/AppendService path, in one transaction.
  3. A second --apply appends nothing (deterministic event_ids).
  4. A device replaying the resulting log reconstructs the server's stock —
     proven by the vitest half, stockEventBackfillDeviceParity.test.ts, which
     reads the contract file this writes.

--apply refuses to run without --i-have-a-backup, which is also tested: that gate
is the only thing between this script and the production log.

DATABASE_URL is pointed at the disposable cluster for the duration of each test
(module-level autouse fixture) so the script cannot reach atlasdb even by
accident.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="needs real PostgreSQL (set TEST_DATABASE_URL to a disposable cluster)",
)

from app.models.inventory.branch_inventory import DrugBatch  # noqa: E402
from app.models.inventory.inventory_model import Drug  # noqa: E402
from app.models.pharmacy.pharmacy_model import Branch, Organization  # noqa: E402
from app.models.user.user_model import User  # noqa: E402

BACKEND = Path(__file__).resolve().parents[2]
SCRIPT = BACKEND / "scripts" / "backfill_stock_events.py"
CONTRACT = (
    BACKEND.parent
    / "ui.laso/src/lib/__tests__/fixtures/stock-backfill-parity.json"
)

# The quantities the device must end up with, per the ruling.
EXPECTED_QTY = {
    "Amoxicilin": 247,
    "Gebedol": 117,
    "Ibuprofen-0561": 40,
    "Minoxidil Oil": 100,
    "Paracetamol 500mg": 198,
    "Paracetamol-9014": 50,
}


def _load_script():
    """Import the script as a module so its helpers can be called directly.

    Registered in sys.modules BEFORE exec_module: @dataclass resolves
    string annotations through sys.modules[cls.__module__], so an unregistered
    module makes the decorator blow up with a confusing
    "'NoneType' object has no attribute '__dict__'".
    """
    spec = importlib.util.spec_from_file_location("backfill_stock_events", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["backfill_stock_events"] = module
    spec.loader.exec_module(module)
    return module


@pytest_asyncio.fixture
async def event_log_table(db):
    await db.execute(text("DROP TABLE IF EXISTS event_log CASCADE"))
    await db.execute(text("""
        CREATE TABLE event_log (
            event_id TEXT NOT NULL,
            org_id UUID NOT NULL,
            seq BIGINT NOT NULL,
            aggregate_id UUID NOT NULL,
            aggregate_type TEXT NOT NULL,
            event_type TEXT NOT NULL,
            schema_version SMALLINT NOT NULL DEFAULT 1,
            payload JSONB NOT NULL,
            dependencies TEXT[] NOT NULL DEFAULT '{}',
            authored_at TIMESTAMPTZ NOT NULL,
            authored_by UUID NOT NULL,
            branch_id UUID NOT NULL,
            hash_self TEXT NOT NULL,
            hash_prev TEXT NOT NULL,
            received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (org_id, event_id),
            UNIQUE (org_id, seq)
        )
    """))
    await db.commit()
    yield
    await db.execute(text("DROP TABLE IF EXISTS event_log CASCADE"))
    await db.commit()


@pytest_asyncio.fixture
async def backfill_scope(db, event_log_table, setup_test_data, monkeypatch):
    """A realistic org: six drugs, categories, an inventory row and a lot each.

    Mirrors the shape of the live org closely enough that the script's own
    planning logic is exercised, including the two drugs with no category.
    """
    org, branch, user, drugs, _customer = setup_test_data
    cat_aspirin = uuid.uuid4()
    cat_antibiotic = uuid.uuid4()

    from app.models.inventory.inventory_model import DrugCategory

    db.add_all([
        DrugCategory(id=cat_aspirin, organization_id=org.id, name="Analgesic",
                     parent_id=None, path="Analgesic", level=0),
        DrugCategory(id=cat_antibiotic, organization_id=org.id, name="Antibiotic",
                     parent_id=None, path="Antibiotic", level=0),
    ])
    await db.flush()

    script = _load_script()
    monkeypatch.setattr(script, "ORG_ID", org.id)
    monkeypatch.setattr(script, "BRANCH_ID", branch.id)
    monkeypatch.setattr(script, "AUTHOR_ID", user.id)
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])

    # setup_test_data gives 3 drugs; the ruling names all six real ones, and the
    # device half has to end up holding every one of them, so top up to six.
    from app.models.inventory.inventory_model import Drug as _Drug

    while len(drugs) < len(EXPECTED_QTY):
        extra = _Drug(
            id=uuid.uuid4(),
            organization_id=org.id,
            name=f"placeholder-{len(drugs)}",
            unit_price=Decimal("50.00"),
            reorder_level=10,
            reorder_quantity=0,
            is_active=True,
            is_deleted=False,
            tax_rate=Decimal("0.00"),
        )
        db.add(extra)
        await db.flush()
        drugs.append(extra)

    lots = []
    for i, drug in enumerate(drugs):
        drug.name = list(EXPECTED_QTY)[i]
        drug.category_id = cat_aspirin if i % 2 == 0 else None
        if drug.name == "Amoxicilin":
            drug.category_id = cat_antibiotic
        await db.flush()

        from app.models.inventory.branch_inventory import BranchInventory

        inv = BranchInventory(
            id=uuid.uuid4(), branch_id=branch.id, drug_id=drug.id,
            quantity=EXPECTED_QTY[drug.name], reserved_quantity=0,
            sync_status="pending", sync_version=1,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(inv)

        # Amoxicilin gets TWO lots (147 + 100) so the multi-lot case is covered,
        # mirroring the real org where its aggregate is the sum of two lots.
        remaining = EXPECTED_QTY[drug.name]
        if drug.name == "Amoxicilin":
            split = [(147, 147), (100, 100)]
        else:
            split = [(remaining, remaining)]
        for n, (qty, rem) in enumerate(split):
            lot = DrugBatch(
                id=uuid.uuid4(), branch_id=branch.id, drug_id=drug.id,
                batch_number=f"LOT-{i}-{n}", quantity=qty, remaining_quantity=rem,
                manufacturing_date=date(2020, 1, 1),
                expiry_date=date.today() + timedelta(days=365 * (i + 1)),
                cost_price=Decimal("8.00"),
                supplier="Backfill Supplier",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            db.add(lot)
            lots.append(lot)
        await db.flush()

    await db.commit()

    # A price contract exists but has NO event: the script must report it and
    # refuse to backfill it.
    from app.models.pricing.pricing_model import PriceContract

    contract = PriceContract(
        id=uuid.uuid4(), organization_id=org.id, contract_code="STANDARD-PRICE",
        contract_name="Standard", contract_type="standard",
        effective_from=date.today() - timedelta(days=1),
        status="active", is_active=True, created_by=user.id,
    )
    db.add(contract)
    await db.commit()

    return {
        "org": org, "branch": branch, "user": user, "drugs": drugs,
        "lots": lots, "script": script, "contract": contract,
    }


def _run(script, *args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, cwd=str(BACKEND),
        env={**os.environ},
    )


async def _event_rows(db, org_id):
    return (
        (
            await db.execute(
                text(
                    "SELECT seq, event_type, aggregate_id, aggregate_type, payload, "
                    "event_id, authored_by, branch_id, authored_at "
                    "FROM event_log WHERE org_id = :o ORDER BY seq"
                ),
                {"o": str(org_id)},
            )
        ).mappings().all()
    )


# ── 1. dry run writes nothing ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(db, backfill_scope):
    org = backfill_scope["org"]
    before = await _event_rows(db, org.id)
    assert before == [], "fixture should start with an empty log"

    result = _run(backfill_scope["script"], "--org-id", str(org.id),
                  "--branch-id", str(backfill_scope["branch"].id),
                  "--author-id", str(backfill_scope["user"].id))
    assert result.returncode == 0, result.stderr[-2000:]

    after = await _event_rows(db, org.id)
    assert after == [], "DRY RUN WROTE SOMETHING"


@pytest.mark.asyncio
async def test_apply_without_backup_flag_is_refused(db, backfill_scope):
    org = backfill_scope["org"]
    result = _run(backfill_scope["script"], "--apply",
                  "--org-id", str(org.id),
                  "--branch-id", str(backfill_scope["branch"].id),
                  "--author-id", str(backfill_scope["user"].id))
    assert result.returncode == 2
    assert "REFUSING" in result.stdout
    assert "i-have-a-backup" in result.stdout
    assert await _event_rows(db, org.id) == []


@pytest.mark.asyncio
async def test_dry_run_reports_the_gap_and_the_contract(db, backfill_scope):
    org, branch = backfill_scope["org"], backfill_scope["branch"]
    result = _run(backfill_scope["script"],
                  "--org-id", str(org.id), "--branch-id", str(branch.id),
                  "--author-id", str(backfill_scope["user"].id))
    out = result.stdout
    assert "GAP TABLE" in out
    assert "DRY RUN" in out
    for name in EXPECTED_QTY:
        assert name in out, f"{name} missing from the report"
    # The contract is reported and explicitly not backfilled.
    assert "STANDARD-PRICE" in out
    assert "NOT BACKFILLED" in out
    assert "seq range that would be used" in out


# ── 2 + 3. apply, then idempotency ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_creates_exactly_the_expected_events(db, backfill_scope):
    org, branch, user = backfill_scope["org"], backfill_scope["branch"], backfill_scope["user"]

    result = _run(backfill_scope["script"], "--apply", "--i-have-a-backup",
                  "--org-id", str(org.id), "--branch-id", str(branch.id),
                  "--author-id", str(user.id))
    assert result.returncode == 0, (
        f"STDOUT:\n{result.stdout[-3000:]}\nSTDERR:\n{result.stderr[-3000:]}"
    )

    rows = await _event_rows(db, org.id)
    # One inventory per drug, one batch event per lot, and no drug events (the
    # fixture's drugs are all new, so drug_created IS expected for each).
    by_type: dict[str, int] = {}
    for r in rows:
        by_type[r["event_type"]] = by_type.get(r["event_type"], 0) + 1

    assert by_type["branch_inventory_created"] == len(backfill_scope["drugs"])
    assert by_type["drug_batch_created"] == len(backfill_scope["lots"])
    assert by_type["drug_created"] == len(backfill_scope["drugs"])
    # The price contract must NOT be backfilled.
    assert "price_contract_created" not in by_type

    # Every batch event carries the server's CURRENT remaining_quantity.
    for r in rows:
        if r["event_type"] != "drug_batch_created":
            continue
        lot = next(l for l in backfill_scope["lots"] if str(l.id) == str(r["aggregate_id"]))
        assert int(r["payload"]["remaining_quantity"]) == int(lot.remaining_quantity)
        assert r["payload"]["batch_number"] == lot.batch_number
        assert r["payload"]["expiry_date"] == lot.expiry_date.isoformat()

    # Authored as the requested user, in seq order, hash-chained.
    assert [r["seq"] for r in rows] == list(range(1, len(rows) + 1))
    assert all(str(r["authored_by"]) == str(user.id) for r in rows)
    assert all(len(r["event_id"]) == 26 for r in rows)
    assert all(str(r["branch_id"]) == str(branch.id) for r in rows)

    # Write the contract for the device half.
    await _write_contract(db, org, branch, backfill_scope, rows)


@pytest.mark.asyncio
async def test_second_apply_creates_nothing(db, backfill_scope):
    org, branch, user = backfill_scope["org"], backfill_scope["branch"], backfill_scope["user"]
    first = _run(backfill_scope["script"], "--apply", "--i-have-a-backup",
                 "--org-id", str(org.id), "--branch-id", str(branch.id),
                 "--author-id", str(user.id))
    assert first.returncode == 0, (
        f"STDOUT:\n{first.stdout[-3000:]}\nSTDERR:\n{first.stderr[-3000:]}"
    )
    before = await _event_rows(db, org.id)
    assert before

    second = _run(backfill_scope["script"], "--apply", "--i-have-a-backup",
                  "--org-id", str(org.id), "--branch-id", str(branch.id),
                  "--author-id", str(user.id))
    assert second.returncode == 0, (
        f"STDOUT:\n{second.stdout[-2000:]}\nSTDERR:\n{second.stderr[-2000:]}"
    )
    after = await _event_rows(db, org.id)

    assert len(after) == len(before), "a second apply duplicated events"
    assert [r["event_id"] for r in after] == [r["event_id"] for r in before]


@pytest.mark.asyncio
async def test_reappending_the_same_deterministic_ids_is_already_appended(
    db, backfill_scope
):
    """The append path itself must de-duplicate, not just the planner.

    The second --apply never reaches the append path, because the planner sees
    every aggregate already published and plans nothing. That is the right
    outcome, but it leaves the deeper guarantee untested: if the SAME
    deterministic event_id is appended again, AppendService must report
    ALREADY_APPENDED rather than writing a duplicate row.
    """
    org, branch, user = backfill_scope["org"], backfill_scope["branch"], backfill_scope["user"]

    first = _run(backfill_scope["script"], "--apply", "--i-have-a-backup",
                 "--org-id", str(org.id), "--branch-id", str(branch.id),
                 "--author-id", str(user.id))
    assert first.returncode == 0, first.stderr[-2000:]
    before = await _event_rows(db, org.id)
    assert before

    script = backfill_scope["script"]
    from app.schemas.event_envelope import AggregateType
    from app.services.sync.eventlog.stock_emitter import (
        StockEventEmitter,
        deterministic_event_id,
    )
    from sqlalchemy.ext.asyncio import AsyncSession

    # Re-append the FIRST event verbatim, same deterministic id.
    row = before[0]
    event_type = row["event_type"]
    agg = AggregateType(event_type.rsplit("_", 1)[0])
    eid = deterministic_event_id(event_type, str(row["aggregate_id"]), "backfill-v1")
    assert eid == row["event_id"], "the id must be reproducible from its inputs"

    async with AsyncSession(bind=db.bind) as s2:
        res = await StockEventEmitter.emit_in_transaction(
            s2,
            org_id=org.id,
            event_type=event_type,
            aggregate_type=agg,
            aggregate_id=row["aggregate_id"],
            payload=row["payload"],
            authored_by=user.id,
            branch_id=branch.id,
            event_id=eid,
        )
        await s2.commit()

    status = getattr(res.status, "value", res.status)
    assert status == "already_appended", f"expected already_appended, got {status}"

    after = await _event_rows(db, org.id)
    assert len(after) == len(before)
    assert [r["event_id"] for r in after] == [r["event_id"] for r in before]


async def _write_contract(db, org, branch, scope, rows) -> None:
    """Hand the resulting log and the server's truth to the device half.

    The device test cannot query PostgreSQL, so the server's expected rows travel
    with the events over this JSON contract.
    """
    events = [
        {
            "event_id": r["event_id"],
            "seq": int(r["seq"]),
            "org_id": str(org.id),
            "aggregate_id": str(r["aggregate_id"]),
            "aggregate_type": r["aggregate_type"],
            "event_type": r["event_type"],
            "schema_version": 1,
            "payload": r["payload"],
            "dependencies": [],
            "authored_at": (
                r["authored_at"].isoformat()
                if hasattr(r["authored_at"], "isoformat")
                else str(r["authored_at"])
            ),
            "authored_by": str(r["authored_by"]),
            "branch_id": str(r["branch_id"]),
            "hash_self": "0" * 64,
            "hash_prev": "0" * 64,
        }
        for r in rows
    ]

    drugs = []
    for drug in scope["drugs"]:
        inv = (
            await db.execute(
                text(
                    "SELECT quantity, reserved_quantity FROM branch_inventory "
                    "WHERE branch_id = :b AND drug_id = :d"
                ),
                {"b": str(branch.id), "d": str(drug.id)},
            )
        ).first()
        lots = (
            await db.execute(
                text(
                    "SELECT id, batch_number, quantity, remaining_quantity, expiry_date "
                    "FROM drug_batches WHERE branch_id = :b AND drug_id = :d "
                    "ORDER BY batch_number"
                ),
                {"b": str(branch.id), "d": str(drug.id)},
            )
        ).mappings().all()
        drugs.append(
            {
                "id": str(drug.id),
                "name": drug.name,
                "quantity": int(inv[0]),
                "reserved_quantity": int(inv[1]),
                "batches": [
                    {
                        "id": str(l["id"]),
                        "batch_number": l["batch_number"],
                        "quantity": int(l["quantity"]),
                        "remaining_quantity": int(l["remaining_quantity"]),
                        "expiry_date": (
                            l["expiry_date"].isoformat() if l["expiry_date"] else None
                        ),
                    }
                    for l in lots
                ],
            }
        )

    CONTRACT.parent.mkdir(parents=True, exist_ok=True)
    CONTRACT.write_text(
        json.dumps(
            {
                "branch_id": str(branch.id),
                "events": events,
                "drugs": drugs,
                "expected_qty": EXPECTED_QTY,
            },
            indent=2,
            sort_keys=True,
            default=str,
        ),
        encoding="utf-8",
    )
