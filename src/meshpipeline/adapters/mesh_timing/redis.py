# Responsibility: Keep recent mesh-duration samples so an estimate can be made from history.
# Boundaries: storage for the timing contract; it enforces no timeout.
from __future__ import annotations

from meshpipeline.adapters._shared.redis_client import sync_client


def _key(engine: str, purpose: str) -> str:
    return f"meshtime:{engine or 'unknown'}:{purpose or 'unknown'}"


class RedisMeshTimingStore:
    def __init__(self) -> None:
        self._redis = None

    def _client(self):
        if self._redis is None:
            self._redis = sync_client()
        return self._redis

    def append_sample(self, engine: str, purpose: str, seconds: float, cap: int) -> None:
        r = self._client()
        k = _key(engine, purpose)
        p = r.pipeline()
        p.lpush(k, f"{seconds:.1f}")
        p.ltrim(k, 0, cap - 1)
        p.execute()

    def read_samples(self, engine: str, purpose: str) -> list[float]:
        raw = self._client().lrange(_key(engine, purpose), 0, -1)
        out: list[float] = []
        for x in raw or []:
            try:
                out.append(float(x))
            except (TypeError, ValueError):
                continue
        return out
