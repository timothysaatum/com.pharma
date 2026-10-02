"""`server_head_seq` — the server must always tell the client the real head.

Why this exists
---------------
`EventPullResponse.next_after_seq` is a paging cursor: when the requested page
comes back empty, it echoes the caller's own `after_seq`. A device whose stored
cursor sat ABOVE the org's real head therefore received nothing, was told its
own cursor back, and concluded it was caught up — while the client-side
`pulledSeq < serverHeadSeq` check compared 225 < 225 and reported "healthy".
The device silently received zero events, forever.

`server_head_seq` is the actual `MAX(seq)` for the organisation, returned on
every response, so the client can tell "you are current" from "you are past my
log" and rewind.

These tests pin the server half of that contract. The client half (rewind to 0
and replay) is covered in ui.laso/src/lib/__tests__/syncEngine.cursorReset.spec.ts.
"""

from __future__ import annotations

import importlib.util
import random
import uuid
from pathlib import Path
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.endpoints.event_sync_endpoints import pull_events
from app.schemas.event_envelope import EventPullResponse
from app.models.pharmacy.pharmacy_model import Organization
from app.models.user.user_model import User

pytestmark = pytest.mark.asyncio


# ── Helpers ──────────────────────────────────────────────────────────────────


# Crockford base32, minus I/L/O/U — the alphabet a ULID uses. `event_log` has a
# CHECK constraint requiring a 26-character event_id, because event ids are
# client-generated ULIDs. The backend has no ULID generator of its own (that
# lives in the client), so the test mints a syntactically valid one.
_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _ulid() -> str:
    return "".join(random.choice(_ULID_ALPHABET) for _ in range(26))


def _load_migration(filename: str):
    """Import a migration module by path (migrations/ has no __init__.py)."""
    path = Path(__file__).resolve().parents[2] / "migrations" / "versions" / filename
    spec = importlib.util.spec_from_file_location(f"_mig_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture(autouse=True)
async def event_log_table(db: AsyncSession):
    """Create `event_log` from its REAL migration DDL.

    `event_log` is defined only in the Alembic migration `e1f2a3b4c5d6`, not as
    an ORM model, so the session fixture's `create_all` does not produce it and
    no existing test exercised the pull endpoint at all.

    Rather than hand-copying the DDL into the test (which would drift silently
    the first time the schema changed), this binds a real Alembic `Operations`
    context to the test connection and runs the actual `upgrade()`. The test
    therefore asserts against the production schema, not a copy of it.
    """
    migration = _load_migration("e1f2a3b4c5d6_add_event_sourced_sync_spine.py")
    conn = await db.connection()

    def _apply(sync_conn):
        # `run_sync` hands the callback a *synchronous* Connection facade, which
        # is what Alembic's Operations needs (its DDL calls conn.execute, not
        # the async coroutine). Point the module's global `op` — normally an
        # ambient proxy — at a real Operations bound to THIS test connection,
        # so the production DDL lands on the test database.
        ctx = MigrationContext.configure(sync_conn)
        migration.op = Operations(ctx)
        migration.upgrade()

    await conn.run_sync(_apply)
    await db.flush()
    yield


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _make_org_with_events(db: AsyncSession, count: int) -> tuple[Organization, User]:
    """Create an org whose event_log holds `count` sequential events."""
    org = Organization(
        id=uuid.uuid4(),
        name=f"Head Org {uuid.uuid4().hex[:6]}",
        type="pharmacy",
        tax_id=f"TAX-{uuid.uuid4().hex[:8]}",
        settings={},
    )
    db.add(org)
    await db.flush()

    user = User(
        id=uuid.uuid4(),
        organization_id=org.id,
        username=f"head_user_{uuid.uuid4().hex[:6]}",
        email=f"head_{uuid.uuid4().hex[:6]}@test.local",
        password_hash="hash",
        full_name="Head User",
        is_super_admin=True,
        is_active=True,
        assigned_branches=[],
    )
    db.add(user)

    for seq in range(1, count + 1):
        await db.execute(
            text(
                """
                INSERT INTO event_log
                    (event_id, org_id, seq, aggregate_id, aggregate_type,
                     event_type, schema_version, payload, dependencies,
                     authored_at, authored_by, branch_id, hash_self,
                     hash_prev, received_at)
                VALUES
                    (:event_id, :org_id, :seq, :agg_id, 'drug',
                     'drug.updated', 1, :payload, :deps,
                     :authored_at, :authored_by, :branch_id, :hash_self,
                     :hash_prev, :authored_at)
                """
            ),
            {
                "event_id": _ulid(),
                "org_id": str(org.id),
                "seq": seq,
                "agg_id": str(uuid.uuid4()),
                "payload": '{"name": "Drug %d"}' % seq,
                "deps": [],
                "authored_at": datetime.now(timezone.utc),
                "authored_by": str(user.id),
                "branch_id": str(uuid.uuid4()),
                "hash_self": "0" * 64,
                "hash_prev": "0" * 64,
            },
        )
    await db.flush()
    return org, user


# ── Tests ────────────────────────────────────────────────────────────────────


async def test_pull_reports_true_head_when_events_returned(db: AsyncSession):
    """With a page of events, the reported head is the org's real MAX(seq)."""
    org, user = await _make_org_with_events(db, count=5)

    response = await pull_events(
        after_seq=0,
        limit=3,
        aggregate_types=None,
        current_user=user,
        db=db,
    )

    assert isinstance(response, EventPullResponse)
    assert len(response.events) == 3
    assert response.has_more is True
    assert response.next_after_seq == 3
    # The client must be able to learn the head even mid-page.
    assert response.server_head_seq == 5


async def test_empty_page_still_reports_head(db: AsyncSession):
    """The empty page is exactly where the old contract was useless."""
    org, user = await _make_org_with_events(db, count=5)

    response = await pull_events(
        after_seq=5,
        limit=100,
        aggregate_types=None,
        current_user=user,
        db=db,
    )

    assert response.events == []
    assert response.has_more is False
    # next_after_seq is just our own cursor echoed back — it cannot be used to
    # detect a stranded cursor.
    assert response.next_after_seq == 5
    # server_head_seq is the truth.
    assert response.server_head_seq == 5


async def test_cursor_above_head_is_detectable(db: AsyncSession):
    """The bug case: a device holding a cursor beyond the log's real head.

    Before this field existed the response was indistinguishable from a healthy
    caught-up device, which is exactly why the device went silent.
    """
    org, user = await _make_org_with_events(db, count=5)

    response = await pull_events(
        after_seq=225,  # stranded: the log only ever reached 5
        limit=100,
        aggregate_types=None,
        current_user=user,
        db=db,
    )

    assert response.events == []
    assert response.next_after_seq == 225  # echoed, useless for detection
    assert response.server_head_seq == 5
    # This inequality IS the self-heal trigger on the client.
    assert 225 > response.server_head_seq


async def test_head_is_scoped_per_organization(db: AsyncSession):
    """One org's head must never be reported to another org."""
    org_a, user_a = await _make_org_with_events(db, count=9)
    org_b, user_b = await _make_org_with_events(db, count=2)

    resp_a = await pull_events(
        after_seq=0, limit=100, aggregate_types=None, current_user=user_a, db=db
    )
    resp_b = await pull_events(
        after_seq=0, limit=100, aggregate_types=None, current_user=user_b, db=db
    )

    assert resp_a.server_head_seq == 9
    assert resp_b.server_head_seq == 2
    # And no cross-org leakage through the event list itself.
    assert all(str(e.org_id) == str(org_a.id) for e in resp_a.events)
    assert all(str(e.org_id) == str(org_b.id) for e in resp_b.events)


async def test_head_is_zero_for_an_organization_with_no_events(db: AsyncSession):
    """An empty log must report 0, not omit the field or raise."""
    org, user = await _make_org_with_events(db, count=0)

    response = await pull_events(
        after_seq=0, limit=100, aggregate_types=None, current_user=user, db=db
    )

    assert response.events == []
    assert response.server_head_seq == 0


async def test_head_reported_with_aggregate_type_filter(db: AsyncSession):
    """A filtered pull still reports the UNFILTERED head.

    The head is the org's log position, not the position within a filter. If the
    filter changed the reported head, a client would misread how far behind it
    is whenever the caller happened to filter.
    """
    org, user = await _make_org_with_events(db, count=7)

    # No events match this filter, but the org's head is still 7.
    response = await pull_events(
        after_seq=0,
        limit=100,
        aggregate_types=["prescription"],
        current_user=user,
        db=db,
    )

    assert response.events == []
    assert response.server_head_seq == 7


async def test_field_is_always_present_in_the_serialised_payload(db: AsyncSession):
    """The wire contract: clients read `server_head_seq` unconditionally."""
    org, user = await _make_org_with_events(db, count=1)

    response = await pull_events(
        after_seq=0, limit=100, aggregate_types=None, current_user=user, db=db
    )

    payload = response.model_dump()
    assert "server_head_seq" in payload
    assert payload["server_head_seq"] == 1
    # Additive with a default, so an older client that ignores it is unaffected.
    assert EventPullResponse.model_fields["server_head_seq"].default == 0