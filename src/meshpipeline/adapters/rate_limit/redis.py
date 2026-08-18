# Responsibility: Count requests per window in Redis for the API's rate limit.
# Boundaries: counting; the limit and the response to exceeding it belong to the API.
from __future__ import annotations

from meshpipeline.adapters._shared.redis_client import async_client


class RedisRateLimitStore:
    def __init__(self) -> None:
        self._redis = None

    def _client(self):
        if self._redis is None:
            self._redis = async_client(socket_connect_timeout=1, socket_timeout=1)
        return self._redis

    async def incr_window(self, identity: str, window: int, ttl_seconds: int) -> int:
        r = self._client()
        key = f"rl:{identity}:{window}"
        n = await r.incr(key)
        if n == 1:
            await r.expire(key, ttl_seconds)
        return int(n)
