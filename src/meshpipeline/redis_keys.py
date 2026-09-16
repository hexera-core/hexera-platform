# Responsibility: Prefix every Redis key with the keyspace this deployment owns.
# Boundaries: naming only - it opens no connection and knows nothing about what is stored.
from __future__ import annotations

import meshpipeline.settings.providers as provcfg


def k(name: str) -> str:
    """Return `name` inside this deployment's Redis keyspace.

    WHY EVERY KEY HAS TO GO THROUGH HERE. Personal environments share one Memorystore instance,
    deliberately - it is what makes a first deploy minutes rather than half an hour. Celery's
    `global_keyprefix` isolates the broker and the result backend, and NOTHING ELSE: the dead
    letter queue, the inference feed, job event logs, websocket tickets, rate-limit counters,
    delivery guards and capacity leases are all addressed by adapters that talk to Redis directly.
    Those keys are literals - `simulation:dlq` and `inference:calls` are the same string in every
    environment - so without this they are one shared bucket that any environment can read, write
    and trim out from under any other, on an instance whose URL carries no authentication.

    EMPTY IS THE DEFAULT and it returns `name` unchanged, which is byte-for-byte what every
    existing deployment already does. Shared dev, production and the local compose stack own their
    instance outright and set nothing.

    Read at CALL time rather than captured at import, so a test that sets the prefix does not have
    to care whether this module was imported first.
    """
    return f"{provcfg.REDIS_KEY_PREFIX}{name}"
