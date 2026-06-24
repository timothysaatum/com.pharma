from fastapi import Request, HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware
from collections import defaultdict
from datetime import datetime, timedelta
import asyncio
from typing import Dict, Tuple

from app.core.config import get_settings
settings = get_settings()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Simple in-memory rate limiting middleware
    For production, consider using Redis or similar
    """

    _cleanup_task: asyncio.Task | None = None

    def __init__(self, app):
        super().__init__(app)
        # Store: {ip_address: [(timestamp, count)]}
        self.requests: Dict[str, list[Tuple[datetime, int]]] = defaultdict(list)
        self.lock = asyncio.Lock()

    async def _ensure_cleanup(self) -> None:
        """Start the cleanup background task once the event loop is running."""
        if RateLimitMiddleware._cleanup_task is None:
            RateLimitMiddleware._cleanup_task = asyncio.create_task(
                self._cleanup_old_entries()
            )
    
    async def dispatch(self, request: Request, call_next):
        """Process request with rate limiting"""

        if not settings.RATE_LIMIT_ENABLED:
            return await call_next(request)

        # Ensure cleanup background task is running
        await self._ensure_cleanup()

        # Skip rate limiting for health check and static files
        if request.url.path in ["/health", "/docs", "/redoc", "/openapi.json"]:
            return await call_next(request)

        # Get client IP
        client_ip = self._get_client_ip(request)
        
        # Check rate limit
        async with self.lock:
            if await self._is_rate_limited(client_ip):
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"Rate limit exceeded. Max {settings.RATE_LIMIT_REQUESTS} requests "
                           f"per {settings.RATE_LIMIT_WINDOW_SECONDS} seconds",
                    headers={
                        "Retry-After": str(settings.RATE_LIMIT_WINDOW_SECONDS)
                    }
                )
            
            # Record this request
            self._record_request(client_ip)
        
        # Process request
        response = await call_next(request)
        
        # Add rate limit headers
        remaining = await self._get_remaining_requests(client_ip)
        response.headers["X-RateLimit-Limit"] = str(settings.RATE_LIMIT_REQUESTS)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(settings.RATE_LIMIT_WINDOW_SECONDS)
        
        return response
    
    def _get_client_ip(self, request: Request) -> str:
        """Extract client IP from request"""
        # Check for forwarded IP (when behind proxy)
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        
        # Check for real IP header
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip
        
        # Fall back to direct client
        return request.client.host if request.client else "unknown"
    
    async def _is_rate_limited(self, client_ip: str) -> bool:
        """Check if client has exceeded rate limit"""
        now = datetime.now()
        window_start = now - timedelta(seconds=settings.RATE_LIMIT_WINDOW_SECONDS)
        
        # Get requests within window
        if client_ip not in self.requests:
            return False
        
        # Filter requests within window
        recent_requests = [
            (ts, count) for ts, count in self.requests[client_ip]
            if ts > window_start
        ]
        
        # Update stored requests
        self.requests[client_ip] = recent_requests
        
        # Count total requests in window
        total_requests = sum(count for _, count in recent_requests)
        
        return total_requests >= settings.RATE_LIMIT_REQUESTS
    
    def _record_request(self, client_ip: str) -> None:
        """Record a request for rate limiting"""
        now = datetime.now()
        self.requests[client_ip].append((now, 1))
    
    async def _get_remaining_requests(self, client_ip: str) -> int:
        """Get remaining requests for client"""
        now = datetime.now()
        window_start = now - timedelta(seconds=settings.RATE_LIMIT_WINDOW_SECONDS)
        
        if client_ip not in self.requests:
            return settings.RATE_LIMIT_REQUESTS
        
        # Count requests in current window
        recent_requests = [
            (ts, count) for ts, count in self.requests[client_ip]
            if ts > window_start
        ]
        
        total_requests = sum(count for _, count in recent_requests)
        remaining = max(0, settings.RATE_LIMIT_REQUESTS - total_requests)
        
        return remaining
    
    async def _cleanup_old_entries(self) -> None:
        """Periodically clean up old rate limit entries"""
        while True:
            await asyncio.sleep(300)  # Run every 5 minutes
            
            async with self.lock:
                now = datetime.now()
                cutoff = now - timedelta(seconds=settings.RATE_LIMIT_WINDOW_SECONDS * 2)
                
                # Remove old entries
                for client_ip in list(self.requests.keys()):
                    self.requests[client_ip] = [
                        (ts, count) for ts, count in self.requests[client_ip]
                        if ts > cutoff
                    ]
                    
                    # Remove empty entries
                    if not self.requests[client_ip]:
                        del self.requests[client_ip]


# ── Per-route rate limiter (FastAPI dependency) ──────────────────────

_per_route_limits: dict[str, list[datetime]] = defaultdict(list)
_route_lock = asyncio.Lock()


async def _cleanup_per_route_limits() -> None:
    """Periodically clean up stale per-route rate-limit entries."""
    while True:
        await asyncio.sleep(300)
        cutoff = datetime.now() - timedelta(seconds=settings.RATE_LIMIT_WINDOW_SECONDS * 2)
        async with _route_lock:
            stale_keys = []
            for key, entries in list(_per_route_limits.items()):
                _per_route_limits[key] = [ts for ts in entries if ts > cutoff]
                if not _per_route_limits[key]:
                    stale_keys.append(key)
            for key in stale_keys:
                del _per_route_limits[key]


# Start the cleanup in the background (module-level singleton)
try:
    asyncio.create_task(_cleanup_per_route_limits())
except RuntimeError:
    pass  # no event loop yet — will be created on first use


def _get_client_ip_from_request(request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip
    return request.client.host if request.client else "unknown"


def rate_limit(max_requests: int = 10, window_seconds: int = 60):
    """
    Dependency factory for per-route per-IP rate limiting.

    Usage:
        @router.post("/login",
                     dependencies=[Depends(rate_limit(5, 60))])
        async def login(request: Request, ...):
            ...
    """
    from fastapi import Request, HTTPException, status

    # Unique key per call to rate_limit()
    _rate_limit_counter[0] += 1
    route_id = f"rl_{_rate_limit_counter[0]}"

    async def _rate_limit_dependency(request: Request) -> None:
        if not settings.RATE_LIMIT_ENABLED:
            return

        client_ip = _get_client_ip_from_request(request)
        store_key = f"{route_id}:{client_ip}"
        now = datetime.now()
        cutoff = now - timedelta(seconds=window_seconds)

        async with _route_lock:
            entries = [ts for ts in _per_route_limits.get(store_key, []) if ts > cutoff]

            if len(entries) >= max_requests:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"Too many requests. Max {max_requests} per {window_seconds} seconds.",
                    headers={"Retry-After": str(window_seconds)},
                )

            entries.append(now)
            _per_route_limits[store_key] = entries

    return _rate_limit_dependency


_rate_limit_counter: list[int] = [0]