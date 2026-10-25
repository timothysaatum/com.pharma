"""Price-contract lifecycle event emission.

The endpoints own the primary write and emit a sync event afterwards. These
tests pin the helper used by create/update/approve/suspend/activate/duplicate:
one event, the full formatted payload, and a swallowed emitter failure.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.api.v1.endpoints import price_contract_endpoints
from app.schemas.event_envelope import AggregateType


@pytest.mark.asyncio
async def test_emit_contract_event_appends_one_event_and_commits():
    db = AsyncMock()
    contract = SimpleNamespace(
        id=uuid.uuid4(), organization_id=uuid.uuid4()
    )
    user = SimpleNamespace(id=uuid.uuid4())
    payload = {"id": str(contract.id), "contract_name": "Standard Pricing"}

    with (
        patch.object(
            price_contract_endpoints.PriceContractService,
            "format_contract_event_payload",
            return_value=payload,
        ) as format_payload,
        patch.object(
            price_contract_endpoints.ServerEventEmitter,
            "emit",
            new=AsyncMock(),
        ) as emit,
    ):
        await price_contract_endpoints._emit_contract_event(
            db, contract, "price_contract_updated", user
        )

    format_payload.assert_called_once_with(contract)
    emit.assert_awaited_once_with(
        db=db,
        org_id=contract.organization_id,
        event_type="price_contract_updated",
        aggregate_type=AggregateType.PRICE_CONTRACT,
        aggregate_id=contract.id,
        payload=payload,
        authored_by=user.id,
    )
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_emit_contract_event_swallows_and_rolls_back_on_failure():
    db = AsyncMock()
    contract = SimpleNamespace(
        id=uuid.uuid4(), organization_id=uuid.uuid4()
    )
    user = SimpleNamespace(id=uuid.uuid4())

    with (
        patch.object(
            price_contract_endpoints.PriceContractService,
            "format_contract_event_payload",
            return_value={"id": str(contract.id)},
        ),
        patch.object(
            price_contract_endpoints.ServerEventEmitter,
            "emit",
            new=AsyncMock(side_effect=RuntimeError("append failed")),
        ),
    ):
        await price_contract_endpoints._emit_contract_event(
            db, contract, "price_contract_updated", user
        )

    db.commit.assert_not_awaited()
    db.rollback.assert_awaited_once()
