import uuid
from datetime import datetime, timedelta, timezone
import pytest
from pydantic import ValidationError

from app.schemas.sync_schemas import PushRequest, PushRecord


def test_push_request_max_records_limit():
    """Bug 7: PushRequest schema must enforce max_length=100 records."""
    dummy_records = [
        PushRecord(
            operation_id=str(uuid.uuid4()),
            table_name="sales",
            operation="create",
            local_id=str(uuid.uuid4()),
            sync_version=1,
            created_offline_at=datetime.now(timezone.utc),
            data={"sale_number": f"SALE-{i}"},
        )
        for i in range(101)
    ]
    with pytest.raises(ValidationError):
        PushRequest(branch_id=uuid.uuid4(), records=dummy_records)


def test_push_record_force_flag_available():
    """Bug 5: PushRecord must support force: bool flag."""
    rec = PushRecord(
        operation_id=str(uuid.uuid4()),
        table_name="sales",
        operation="create",
        local_id=str(uuid.uuid4()),
        sync_version=1,
        created_offline_at=datetime.now(timezone.utc) - timedelta(days=10),
        force=True,
        data={"sale_number": "OFFLINE-OLD-1"},
    )
    assert rec.force is True
