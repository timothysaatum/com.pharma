"""
Integration coverage for the security-headers middleware registered in
app/core/middleware_config.py (add_security_headers_middleware).

Modeled on tests/integration/test_reports_endpoints.py's
AsyncClient/ASGITransport pattern. Hits the unauthenticated GET /health
endpoint so no auth/dependency overrides are needed.
"""
import pytest
from httpx import AsyncClient, ASGITransport

from main import app


@pytest.mark.asyncio
async def test_health_response_has_frame_and_content_type_headers():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/health")

    assert resp.status_code == 200
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.asyncio
async def test_hsts_absent_on_plain_http_request():
    """Plain-HTTP dev/local traffic must not be told to upgrade to HTTPS."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/health")

    assert resp.status_code == 200
    assert "Strict-Transport-Security" not in resp.headers


@pytest.mark.asyncio
async def test_hsts_present_on_https_scoped_request():
    """When the request is actually HTTPS-scoped, HSTS must be sent."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as ac:
        resp = await ac.get("/health")

    assert resp.status_code == 200
    assert resp.headers["Strict-Transport-Security"] == "max-age=63072000; includeSubDomains"
    # HSTS is additive to the baseline headers, not a replacement for them.
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
