import uuid

import pytest

from app.api.v1.endpoints.sync_endpoints import _user_can_sync_branch
from app.schemas.sync_schemas import PullRequest, PullResponse
from app.services.sync.sync_service import SyncService


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
