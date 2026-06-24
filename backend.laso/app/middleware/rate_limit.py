import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Protocol, Tuple
import json

from fastapi import Request, HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


# ── Backend Protocol ──────────────────────────────────────────────────────────

class RateLimitBackend(Protocol):
    """Interface for rate-limit storage backends."""
    async def is_limited(self, key: str, max_requests: int, window_seconds: int) -> bool: ...
    async def record(self, key: str, window_seconds: int) -> None: ...
    async def remaining(self, key: str, max_requests: int, window_seconds: int) -> int: ...
    async def close(self) -> None: ...


# ── In-Memory Backend (fallback / single-worker) ──────────────────────────

class MemoryBackend:
    def __init__(self) -> None:
        self._store: Dict[str, List[Tuple[datetime, int]]] = defaultdict(list)
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None

    async def _ensure_cleanup(self) -> None:
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(300)
            cutoff = datetime.now() - timedelta(seconds=settings.RATE_LIMIT_WINDOW_SECONDS * 2)
            async with self._lock:
                for ip in list(self._store.keys()):
                    self._store[ip] = [(ts, c) for ts, c in self._store[ip] if ts > cutoff]
                    if not self._store[ip]:
                        del self._store[ip]

    async def is_limited(self, key: str, max_requests: int, window_seconds: int) -> bool:
        await self._ensure_cleanup()
        async with self._lock:
            now = datetime.now()
            cutoff = now - timedelta(seconds=window_seconds)
            entries = [(ts, c) for ts, c in self._store.get(key, []) if ts > cutoff]
            self._store[key] = entries
            return sum(c for _, c in entries) >= max_requests

    async def record(self, key: str, window_seconds: int) -> None:
        async with self._lock:
            self._store[key].append((datetime.now(), 1))

    async def remaining(self, key: str, max_requests: int, window_seconds: int) -> int:
        async with self._lock:
            now = datetime.now()
            cutoff = now - timedelta(seconds=window_seconds)
            entries = [(ts, c) for ts, c in self._store.get(key, []) if ts > cutoff]
            total = sum(c for _, c in entries)
            return max(0, max_requests - total)

    async def close(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()


# ── Redis Backend (multi-worker) ─────────────────────────────────────────

class RedisBackend:
    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._redis = None
        self._connect_error: Optional[str] = None

    async def _get_redis(self):
        if self._redis is not None:
            return self._redis
        if self._connect_error:
            return None
        try:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(
                self._redis_url,
                socket_connect_timeout=2,
                socket_timeout=2,
                decode_responses=True,
            )
            await self._redis.ping()
            logger.info("Redis rate-limit backend connected")
        except Exception as exc:
            self._connect_error = str(exc)
            logger.warning("Redis unavailable for rate limiting (%s); rate limiting disabled", exc)
            self._redis = None
        return self._redis

    async def is_limited(self, key: str, max_requests: int, window_seconds: int) -> bool:
        r = await self._get_redis()
        if r is None:
            return False
        try:
            val = await r.get(key)
            return (int(val) if val else 0) >= max_requests
        except Exception:
            return False

    async def record(self, key: str, window_seconds: int) -> None:
        r = await self._get_redis()
        if r is None:
            return
        try:
            pipe = r.pipeline()
            pipe.incr(key)
            pipe.expire(key, window_seconds)
            await pipe.execute()
        except Exception:
            pass

    async def remaining(self, key: str, max_requests: int, window_seconds: int) -> int:
        r = await self._get_redis()
        if r is None:
            return max_requests
        try:
            val = await r.get(key)
            current = int(val) if val else 0
            return max(0, max_requests - current)
        except Exception:
            return max_requests

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()


# ── Backend Factory ──────────────────────────────────────────────────────

_backend: Optional[RateLimitBackend] = None
_init_lock = asyncio.Lock()


async def _get_backend() -> RateLimitBackend:
    global _backend
    if _backend is not None:
        return _backend
    async with _init_lock:
        if _backend is not None:
            return _backend
        if settings.REDIS_URL:
            _backend = RedisBackend(settings.REDIS_URL)
        if _backend is None:
            _backend = MemoryBackend()
    return _backend


# ── Middleware ───────────────────────────────────────────────────────────

class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not settings.RATE_LIMIT_ENABLED:
            return await call_next(request)

        if request.url.path in ("/health", "/docs", "/redoc", "/openapi.json"):
            return await call_next(request)

        backend = await _get_backend()
        client_ip = _get_client_ip(request)
        window = settings.RATE_LIMIT_WINDOW_SECONDS
        max_req = settings.RATE_LIMIT_REQUESTS
        key = f"rl:global:{client_ip}"

        limited = await backend.is_limited(key, max_req, window)
        if limited:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded. Max {max_req} requests per {window} seconds",
                headers={"Retry-After": str(window)},
            )

        await backend.record(key, window)
        response = await call_next(request)

        remaining = await backend.remaining(key, max_req, window)
        response.headers["X-RateLimit-Limit"] = str(max_req)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(window)
        return response


# ── Per-route rate limiter (FastAPI dependency) ──────────────────────────

_route_backend: Optional[RateLimitBackend] = None
_route_init_lock = asyncio.Lock()


async def _get_route_backend() -> RateLimitBackend:
    global _route_backend
    if _route_backend is not None:
        return _route_backend
    async with _route_init_lock:
        if _route_backend is not None:
            return _route_backend
        _route_backend = RedisBackend(settings.REDIS_URL) if settings.REDIS_URL else MemoryBackend()
    return _route_backend


_rate_limit_counter: List[int] = [0]


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip
    return request.client.host if request.client else "unknown"


def rate_limit(max_requests: int = 10, window_seconds: int = 60):
    _rate_limit_counter[0] += 1
    route_id = f"rl_{_rate_limit_counter[0]}"

    async def _rate_limit_dependency(request: Request) -> None:
        if not settings.RATE_LIMIT_ENABLED:
            return
        backend = await _get_route_backend()
        client_ip = _get_client_ip(request)
        key = f"{route_id}:{client_ip}"

        limited = await backend.is_limited(key, max_requests, window_seconds)
        if limited:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many requests. Max {max_requests} per {window_seconds} seconds.",
                headers={"Retry-After": str(window_seconds)},
            )
        await backend.record(key, window_seconds)

    return _rate_limit_dependency
