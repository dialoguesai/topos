from __future__ import annotations

import time
from typing import Dict

from fastapi import HTTPException, Request, status

from .config.settings import settings


class TokenBucket:
    def __init__(self, rate_per_minute: int) -> None:
        self.capacity = rate_per_minute
        self.tokens = rate_per_minute
        self.refill_time = time.time()
        self.rate_per_second = rate_per_minute / 60.0

    def consume(self, tokens: int = 1) -> bool:
        now = time.time()
        elapsed = now - self.refill_time
        refill = elapsed * self.rate_per_second
        if refill > 0:
            self.tokens = min(self.capacity, self.tokens + refill)
            self.refill_time = now
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False


_buckets: Dict[str, TokenBucket] = {}


def rate_limit(request: Request) -> None:
    """Simple in-memory rate limit per client IP."""
    ip = request.client.host if request.client else "unknown"
    bucket = _buckets.get(ip)
    if bucket is None:
        bucket = TokenBucket(settings.rate_limit_per_minute)
        _buckets[ip] = bucket

    if not bucket.consume():
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many requests")
