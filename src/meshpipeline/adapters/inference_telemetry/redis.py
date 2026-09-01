# Responsibility: Keep every costed model call, so a job's model spend can be read back.
# Boundaries: the Redis implementation of the inference-telemetry contract; it prices nothing and bills nothing.
from __future__ import annotations

import json
from typing import Any

from meshpipeline.adapters._shared.redis_client import sync_client
from meshpipeline.contracts.inference_telemetry import InferenceCall

#: The whole-deployment feed, newest first: what ran, on which model, at what cost. Capped, like
#: the dead-letter queue, because this is an inspection surface and not the ledger.
_FEED_KEY = "inference:calls"
_FEED_KEEP = 5000

#: The per-job slice. Cost is billed per job, so a job's calls have to be readable without
#: scanning a shared feed a busy deployment would have already trimmed them out of.
_JOB_KEEP = 1000
_JOB_TTL_S = 7 * 24 * 3600


def _job_key(job_id: str) -> str:
    # A call made outside a job still spends money, so it is stored rather than dropped - under a
    # named key, because "job::inference" would be an empty name that no operator can ask for.
    return f"job:{job_id or 'unattributed'}:inference"


class RedisInferenceTelemetrySink:
    def __init__(self) -> None:
        self._redis = None

    def _client(self):
        # Cached, unlike the dead-letter sink: this runs on EVERY model call, so opening a
        # connection per record would pay a handshake for every turn of every agent.
        if self._redis is None:
            self._redis = sync_client()
        return self._redis

    def record(self, call: InferenceCall) -> None:
        payload = json.dumps(call.as_record(), default=str)
        job = _job_key(call.job_id)
        try:
            p = self._client().pipeline()
            p.lpush(_FEED_KEY, payload)
            p.ltrim(_FEED_KEY, 0, _FEED_KEEP - 1)
            p.lpush(job, payload)
            p.ltrim(job, 0, _JOB_KEEP - 1)
            p.expire(job, _JOB_TTL_S)
            p.execute()
        except Exception:
            # A dropped connection must not be cached into every later call: the next record
            # dials again. The contract's recorder logs this and the measured call proceeds.
            self._redis = None
            raise

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._read(_FEED_KEY, limit)

    def for_job(self, job_id: str, limit: int = _JOB_KEEP) -> list[dict[str, Any]]:
        return self._read(_job_key(job_id), limit)

    def _read(self, key: str, limit: int) -> list[dict[str, Any]]:
        raw = self._client().lrange(key, 0, max(0, limit - 1))
        out: list[dict[str, Any]] = []
        for item in raw or []:
            try:
                out.append(json.loads(item))
            except (json.JSONDecodeError, TypeError):
                continue        # a malformed record must not hide the readable ones
        return out
