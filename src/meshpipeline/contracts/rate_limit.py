# Responsibility: Declare the windowed counter the API rate limits against.
# Boundaries: a Protocol and its binding; the implementation lives in adapters/rate_limit/.
from __future__ import annotations

from typing import Protocol, runtime_checkable


class RateLimitError(RuntimeError):
    pass


@runtime_checkable
class RateLimitStore(Protocol):
    async def incr_window(self, identity: str, window: int, ttl_seconds: int) -> int:
        ...


_store: RateLimitStore | None = None


def set_rate_limit_store(store: RateLimitStore | None) -> None:
    global _store
    _store = store


async def incr_window(identity: str, window: int, ttl_seconds: int) -> int:
    if _store is None:
        raise RateLimitError(
            "no rate-limit store configured - runtime composition must call set_rate_limit_store()")
    return await _store.incr_window(identity, window, ttl_seconds)
