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
    
    def __init__(self, app):
        super().__init__(app)
        # Store: {ip_address: [(timestamp, count)]}
        self.requests: Dict[str, list[Tuple[datetime, int]]] = defaultdict(list)
        self.lock = asyncio.Lock()
        
        # Start cleanup task
        asyncio.create_task(self._cleanup_old_entries())
    
    async def dispatch(self, request: Request, call_next):
        """Process request with rate limiting"""
        
        if not settings.RATE_LIMIT_ENABLED:
            return await call_next(request)
        
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


# Decorator for route-specific rate limiting
def rate_limit(max_requests: int = 10, window_seconds: int = 60):
    """
    Decorator for custom rate limiting on specific routes
    
    Usage:
        @router.post("/login")
        @rate_limit(max_requests=5, window_seconds=300)
        async def login(...):
            ...
    """
    def decorator(func):
        # Store rate limit config on function
        func._rate_limit_max = max_requests
        func._rate_limit_window = window_seconds
        return func
    return decorator