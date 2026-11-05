# Responsibility: Hand out the shared Redis connections every Redis-backed adapter uses.
# Boundaries: connection construction only; it knows nothing about what any adapter stores.
from __future__ import annotations

from typing import Any

import meshpipeline.settings.providers as provcfg


def sync_client(**kwargs: Any):
    import redis

    return redis.from_url(provcfg.REDIS_URL, decode_responses=True, **kwargs)


def async_client(**kwargs: Any):
    import redis.asyncio as aioredis

    return aioredis.from_url(provcfg.REDIS_URL, **kwargs)
