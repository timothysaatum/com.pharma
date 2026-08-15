from __future__ import annotations

import pytest
from httpx import AsyncClient, ASGITransport
from main import app


@pytest.mark.asyncio
async def test_options_cors_preflight_request_returns_200():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.options(
            "/api/v1/inventory/branch/31a6f154-ddf8-4a22-aec8-3855c4368f2d",
            headers={
                "Origin": "http://localhost:1420",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization, Content-Type",
            },
        )
        assert response.status_code == 200
        assert "access-control-allow-origin" in response.headers
