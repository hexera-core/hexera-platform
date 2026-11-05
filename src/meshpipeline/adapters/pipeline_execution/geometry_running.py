# Responsibility: Say in Redis, for as long as a geometry check is running on a worker, that it is - one key
# per running check, expiring by itself - so the fleet's demand counts checks in flight, not only checks
# waiting.
# Boundaries: an adapter (it talks to Redis). It marks and unmarks; it never fails or slows the check (every
# Redis error is swallowed), and it decides nothing about scaling: deploy/gcp/worker/queue_depth_publisher.py
# counts the keys, and the autoscaler sizes the fleet on what it publishes.
# Collaborates with: adapters/pipeline_execution/celery.py (wraps the two geometry tasks), the publisher
# (RUNNING_CHECK_KEY_TEMPLATE is restated there; tests/unit/deploy/test_geometry_check_queue.py holds the two
# together), application/geometry_check.queue_position (reads the same keys for the console's line).
"""RUNNING GEOMETRY CHECKS.

Shared dev, 1 Nov: two checks ran at a time on the one worker (two slots), and the autoscaler never heard
of them - its demand metric counted the simulation queue alone, so a backlog of checks waited for a slot
however long it grew, and a fleet at zero would never wake for one. The queue's depth says what is
WAITING; this says what is RUNNING, the way a mesh job's execution fence does for the simulation queue.

A key per running task, refreshed every RUNNING_REFRESH_S while it runs and living RUNNING_TTL_S: a worker
killed mid-check stops refreshing, and its key is gone within a minute and a half, with nobody having to
notice.
"""
from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable, Iterator
from typing import Any

logger = logging.getLogger(__name__)

#: `<REDIS_KEY_PREFIX>fleet:geometry-running:<task id>` - restated in the publisher, which may import nothing.
RUNNING_CHECK_KEY_TEMPLATE = "fleet:geometry-running:{task_id}"
RUNNING_TTL_S = 90
RUNNING_REFRESH_S = 30.0


def running_key(prefix: str, task_id: str) -> str:
    return prefix + RUNNING_CHECK_KEY_TEMPLATE.format(task_id=task_id)


def running_pattern(prefix: str) -> str:
    escaped = "".join("\\" + ch if ch in "*?[]\\" else ch for ch in prefix)
    return escaped + RUNNING_CHECK_KEY_TEMPLATE.format(task_id="*")


def _default_client() -> Any:
    from meshpipeline.adapters._shared.redis_client import sync_client
    return sync_client(socket_timeout=5, socket_connect_timeout=5)


def _prefix() -> str:
    try:
        from meshpipeline.settings.providers import REDIS_KEY_PREFIX
        return str(REDIS_KEY_PREFIX or "")
    except Exception:  # noqa: BLE001 - no prefix configured
        return ""


@contextlib.contextmanager
def running(task_id: str, *, client_factory: Callable[[], Any] | None = None,
            prefix: str | None = None) -> Iterator[None]:
    """Mark `task_id` as a running geometry check for the duration of the block. Never raises."""
    stop = threading.Event()
    key = running_key(_prefix() if prefix is None else prefix, str(task_id or "unnamed"))
    client = None
    try:
        client = (client_factory or _default_client)()
        client.set(key, "1", ex=RUNNING_TTL_S)
    except Exception as exc:  # noqa: BLE001 - the check runs whatever Redis says
        logger.info("geometry running mark: not set (%s)", type(exc).__name__)
        client = None

    def refresh() -> None:
        while not stop.wait(RUNNING_REFRESH_S):
            try:
                if client is not None:
                    client.set(key, "1", ex=RUNNING_TTL_S)
            except Exception:  # noqa: BLE001, S112 - try again next time
                continue

    thread = threading.Thread(target=refresh, name="geometry-running", daemon=True) if client is not None else None
    if thread is not None:
        thread.start()
    try:
        yield
    finally:
        stop.set()
        if client is not None:
            try:
                client.delete(key)
            except Exception:  # noqa: BLE001 - it expires by itself
                pass


def count_running(client: Any, prefix: str = "") -> int:
    """How many geometry checks are running now (SCAN, never KEYS)."""
    return len(set(client.scan_iter(match=running_pattern(prefix), count=500)))


#: The geometry-check queue's Redis list (celery_app.py task_routes), and how far down it the console's line
#: looks for a check (a longer queue says "behind N" from its length).
QUEUE = "geometry_checks"
MAX_SCANNED = 500


def queue_state(task_id: str, *, client_factory: Callable[[], Any] | None = None,
                prefix: str | None = None) -> dict | None:
    """Where one geometry task stands (contracts/geometry_check.queue_state): {"running": True} while a worker
    runs it, {"ahead": N} while it waits (N tasks will be taken before it - Celery's Redis transport pushes on
    the left and takes from the right), None when it is in neither place (just taken, or done)."""
    import json

    pre = _prefix() if prefix is None else prefix
    client = (client_factory or _default_client)()
    if client.exists(running_key(pre, task_id)):
        return {"running": True}
    queue = pre + QUEUE
    total = int(client.llen(queue))
    if total <= 0:
        return None
    raw = client.lrange(queue, max(0, total - MAX_SCANNED), -1)
    for i, item in enumerate(raw):
        try:
            message = json.loads(item)
        except (TypeError, ValueError):
            continue
        if str((message.get("headers") or {}).get("id") or "") == str(task_id):
            # index i counts from the left of the slice; everything to its right is taken first
            return {"ahead": len(raw) - 1 - i}
    return None


def waiting(*, client_factory: Callable[[], Any] | None = None, prefix: str | None = None) -> int:
    """How many geometry tasks wait in the queue for a slot now; 0 when Redis cannot say (never raises)."""
    try:
        pre = _prefix() if prefix is None else prefix
        return int((client_factory or _default_client)().llen(pre + QUEUE))
    except Exception as exc:  # noqa: BLE001 - unread: nothing is waiting, as far as this can tell
        logger.info("geometry queue length: not read (%s)", type(exc).__name__)
        return 0


__all__ = ["QUEUE", "waiting", "RUNNING_CHECK_KEY_TEMPLATE", "queue_state", "RUNNING_REFRESH_S", "RUNNING_TTL_S", "count_running", "running",
           "running_key", "running_pattern"]
