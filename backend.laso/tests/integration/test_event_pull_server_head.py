"""
server_head_seq must be the true per-org head on every pull response.

The defect: the pull response carried no head at all, so the client inferred one
from next_after_seq, which echoes after_seq unchanged when a page comes back
empty. A device whose cursor had drifted above the real head read its own cursor
back as "the head" and reported itself perfectly in sync while never advancing.

Requires TEST_DATABASE_URL pointing at a DISPOSABLE cluster: tests/conftest.py
issues DROP SCHEMA ... CASCADE.
"""
import os
import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy import text

from app.schemas.event_envelope import GENESIS_HASH

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="needs real PostgreSQL (set TEST_DATABASE_URL to a disposable cluster)",
)


@pytest_asyncio.fixture
async def event_log_table(db):
    """Create event_log for the duration of a test.

    event_log has no ORM model on purpose: the sync spine is owned by Alembic
    (migration e1f2a3b4c5d6), so Base.metadata.create_all never builds it and the
    tests that need it create it with raw DDL. This mirrors the convention in
    test_event_sync_spine.py.
    """
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
    await db.execute(text(
        "CREATE INDEX ix_event_log_pull_cursor ON event_log (org_id, seq)"
    ))
    await db.commit()
    yield
    await db.execute(text("DROP TABLE IF EXISTS event_log CASCADE"))
    await db.commit()


async def _insert_event(db, *, org_id, seq, aggregate_id, authored_by, branch_id) -> None:
    """Insert a schema-valid event row directly.

    event_id must be a 26-character ULID and the hashes 64 hex characters, or
    EventEnvelope.model_validate rejects the row and the pull returns 500.
    """
    await db.execute(
        text(
            """
            INSERT INTO event_log
              (event_id, org_id, seq, aggregate_id, aggregate_type, event_type,
               schema_version, payload, authored_at, authored_by, branch_id,
               hash_self, hash_prev)
            VALUES (:eid, :org, :seq, :agg, 'drug', 'drug_created', 1,
                    CAST(:payload AS jsonb), now(), :author, :branch,
                    :hash_self, :hash_prev)
            """
        ),
        {
            "eid": _new_ulid(),
            "org": str(org_id),
            "seq": seq,
            "agg": str(aggregate_id),
            "payload": '{"name":"Gebedol","unit_price":5.0}',
            "author": str(authored_by),
            "branch": str(branch_id),
            "hash_self": "a" * 64,
            "hash_prev": GENESIS_HASH,
        },
    )


def _client(user, db_session):
    from main import app
    from app.core import deps

    async def _user():
        return user

    async def _db():
        yield db_session

    app.dependency_overrides[deps.get_current_user] = _user
    app.dependency_overrides[deps.get_db] = _db
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _pull(db, user, **params):
    # The sync routes go through get_current_active_user, which rejects a user
    # flagged must_change_password (deps.py:163). The shared fixture user has
    # the column default, so clear it for these requests.
    user.must_change_password = False
    async with _client(user, db) as client:
        return await client.get("/api/v1/sync/events", params=params)


@pytest.mark.asyncio
async def test_empty_page_still_reports_the_true_head(db, event_log_table, setup_test_data):
    """The empty page is exactly where the old code told the device it was synced.

    after_seq is far past the org's last event, so the page is empty and
    next_after_seq echoes the cursor. server_head_seq must still be the real head.
    """
    org, _branch, user, _drugs, _customer = setup_test_data

    await _insert_event(
        db,
        org_id=org.id,
        seq=1,
        aggregate_id=uuid.uuid4(),
        authored_by=user.id,
        branch_id=uuid.uuid4(),
    )
    await db.commit()

    # Cursor far ahead of the org's single event: page is empty.
    resp = await _pull(db, user, after_seq=225, limit=200)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["events"] == []
    # The echo that caused the bug is still there, unchanged.
    assert body["next_after_seq"] == 225
    # And the truth is now available alongside it.
    assert body["server_head_seq"] == 1, body


@pytest.mark.asyncio
async def test_head_is_zero_for_an_org_with_no_events(db, event_log_table, setup_test_data):
    _org, _branch, user, _drugs, _customer = setup_test_data

    resp = await _pull(db, user, after_seq=0, limit=200)
    assert resp.status_code == 200, resp.text
    assert resp.json()["server_head_seq"] == 0


@pytest.mark.asyncio
async def test_head_is_scoped_per_organization(db, event_log_table, setup_test_data):
    """Two orgs, different heads: each pull must see only its own."""
    org_a, _branch, user_a, _drugs, _customer = setup_test_data
    org_b_id = uuid.uuid4()

    for seq in range(1, 4):          # org A head = 3
        await _insert_event(
            db, org_id=org_a.id, seq=seq, aggregate_id=uuid.uuid4(),
            authored_by=user_a.id, branch_id=uuid.uuid4(),
        )
    await _insert_event(
        db, org_id=org_b_id, seq=1, aggregate_id=uuid.uuid4(),
        authored_by=user_a.id, branch_id=uuid.uuid4(),
    )
    await db.commit()

    resp_a = await _pull(db, user_a, after_seq=0, limit=200)
    assert resp_a.json()["server_head_seq"] == 3
    assert len(resp_a.json()["events"]) == 3

    # Same server, a caller belonging to org B.
    from types import SimpleNamespace

    user_b = SimpleNamespace(
        organization_id=org_b_id,
        is_super_admin=True,
        assigned_branches=[],
        has_permission=lambda _p: True,
        id=user_a.id,
        must_change_password=False,
    )
    resp_b = await _pull(db, user_b, after_seq=0, limit=200)
    assert resp_b.json()["server_head_seq"] == 1
    assert len(resp_b.json()["events"]) == 1


@pytest.mark.asyncio
async def test_existing_fields_are_unchanged(db, event_log_table, setup_test_data):
    """server_head_seq is additive: no existing field changes shape."""
    _org, _branch, user, _drugs, _customer = setup_test_data

    resp = await _pull(db, user, after_seq=0, limit=200)
    body = resp.json()
    for field in ("server_clock", "events", "has_more", "next_after_seq"):
        assert field in body, f"{field} disappeared"
    assert body["has_more"] is False


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _new_ulid() -> str:
    """A 26-character Crockford ULID, which is what EventEnvelope requires."""
    import time

    ms = int(time.time() * 1000)
    ts = "".join(_CROCKFORD[(ms >> (5 * i)) & 0x1F] for i in range(9, -1, -1))
    rand = "".join(_CROCKFORD[uuid.uuid4().int % 32] for _ in range(16))
    return ts + rand