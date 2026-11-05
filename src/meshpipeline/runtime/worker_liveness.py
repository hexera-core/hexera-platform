# Responsibility: Say whether a celery worker's consumer is alive - a heartbeat its own event loop writes, the check docker runs on it, and the presence the queue publisher counts.
# Owns: the heartbeat file, the staleness rule, the `check` command, and the per-queue presence keys in Redis.
# Boundaries: it reports; restarting a stuck container is the VM watchdog's job (deploy/gcp/worker/startup.sh), and the beat itself is driven from runtime/celery_worker.py.
"""Worker liveness.

WHY. Shared dev, 2026-10-16 06:58 UTC: Redis reset the geometry worker's broker connection
(ConnectionError 104). The worker logged "consumer: Connection to broker lost. Trying to
re-establish the connection..." and then nothing, ever. The process stayed up, so docker's
`--restart always` never fired, and no geometry check ran on shared dev for three days. The
simulation worker on the same VM sat in the same state: its idle self-retire asked the consumer to
stop taking work every two minutes and was told "did not stop taking work within 10s", because the
loop that would have done it was not running. A live process is not a working worker.

WHAT COUNTS AS ALIVE. The consumer's own event loop runs a timer that touches a file
(runtime/celery_worker.py, ConsumerHeartbeat). That timer fires only while the loop is turning, and
the loop turns only while the consumer is connected and reading from the broker: during a
reconnect, a blocked socket read, or a wedged loop it stops. So the age of that file is exactly
"how long since this consumer last worked". A job running in a prefork pool process does not stop
the main process's loop, so a long mesh job is not mistaken for a hang.

WHAT ACTS ON IT. `python -m meshpipeline.runtime.worker_liveness check` is the container's docker
healthcheck (deploy/gcp/worker/startup.sh): it exits 1 once the beat is older than
STALE_AFTER_SECONDS. Docker does not restart an unhealthy container by itself, so the VM's
watchdog restarts it. The healthcheck's start period covers the time before the first beat.

PRESENCE. A beat also records which queues the consumer is ACTUALLY consuming right now. A small
thread copies that into Redis - one key per queue and worker, expiring on its own - only while the
beat is fresh. deploy/gcp/worker/queue_depth_publisher.py counts those keys, so a queue with work
waiting and nobody consuming it is said out loud instead of discovered three days later.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: How often the consumer's loop beats.
BEAT_SECONDS = 10.0
#: A beat older than this means the consumer has stopped working. Long enough to ride out an
#: ordinary reconnect (a few seconds) and a slow loop iteration; short enough that a wedged worker
#: is restarted within minutes, not days.
STALE_AFTER_SECONDS = 120.0
#: How often the presence thread refreshes the Redis keys, and how long each one lives.
PRESENCE_SECONDS = 20.0
PRESENCE_TTL_SECONDS = 60
#: The keys deploy/gcp/worker/queue_depth_publisher.py counts, under the deployment's keyspace:
#: `<REDIS_KEY_PREFIX>fleet:consumers:<queue>:<node>`. Restated there (it may use only the standard
#: library and the redis client); tests/unit/runtime/test_worker_liveness.py holds the two together.
PRESENCE_KEY_TEMPLATE = "fleet:consumers:{queue}:{node}"

_HEARTBEAT_PREFIX = "hexera-consumer-heartbeat."


def heartbeat_path(pid: int | None = None) -> Path:
    """The heartbeat of the worker whose MAIN process is `pid` (default: this process).

    Named after the pid for the same reason as the drain marker (runtime/worker_drain.py): two
    workers on one machine never vouch for each other. In a container the main process is PID 1,
    which is what `check` reads by default."""
    return Path(tempfile.gettempdir()) / f"{_HEARTBEAT_PREFIX}{int(pid if pid is not None else os.getpid())}"


class _State:
    """The last beat, as the presence thread reads it."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.at: float | None = None
        self.node = ""
        self.queues: tuple[str, ...] = ()


_STATE = _State()


def clear_heartbeat(pid: int | None = None) -> None:
    """Remove a heartbeat a previous run of this container left under the same pid. A container
    restarted in place keeps its filesystem, and a fresh-looking file from the run before would
    vouch for a consumer that has not connected yet."""
    try:
        heartbeat_path(pid).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("worker liveness: could not clear the old heartbeat (%s)", exc)


def beat(*, node: str, queues: Sequence[str], pid: int | None = None,
         clock: Callable[[], float] = time.time) -> None:
    """Called from the consumer's event loop: this consumer is turning, and consuming `queues`.

    Never raises - a liveness beat that broke the loop it reports on would be worse than none."""
    now = clock()
    try:
        path = heartbeat_path(pid)
        path.write_text(json.dumps({"at": now, "node": node, "queues": list(queues)}),
                        encoding="utf-8")
    except OSError as exc:
        logger.warning("worker liveness: could not write the heartbeat (%s)", exc)
    with _STATE.lock:
        _STATE.at = now
        _STATE.node = node
        _STATE.queues = tuple(queues)


def heartbeat_age(pid: int | None = None, *, clock: Callable[[], float] = time.time) -> float | None:
    """Seconds since the consumer last beat; None when it never has (or the file is unreadable)."""
    try:
        return max(0.0, clock() - heartbeat_path(pid).stat().st_mtime)
    except OSError:
        return None


def verdict(age: float | None, stale_after: float = STALE_AFTER_SECONDS) -> tuple[bool, str]:
    """(healthy, why) - the words go into docker's health log, which is what an operator reads."""
    if age is None:
        return False, "the consumer has not beaten yet (still connecting, or stuck before its first loop)"
    if age > stale_after:
        return False, (f"the consumer last beat {age:.0f}s ago (limit {stale_after:.0f}s) - its event "
                       f"loop is not running: a lost broker connection it never got back, or a hang")
    return True, f"the consumer beat {age:.0f}s ago"


# ---------------------------------------------------------------------------------------------
# presence in Redis
# ---------------------------------------------------------------------------------------------

def presence_key(prefix: str, queue: str, node: str) -> str:
    return prefix + PRESENCE_KEY_TEMPLATE.format(queue=queue, node=node)


def publish_presence(client: Any, *, prefix: str, clock: Callable[[], float] = time.time,
                     stale_after: float = STALE_AFTER_SECONDS) -> int:
    """Refresh one key per queue this consumer is consuming - only while its beat is fresh.

    A stale beat writes nothing, so the keys expire and the publisher counts nobody: the presence
    of a worker that stopped working disappears by itself, with nothing having to notice it."""
    with _STATE.lock:
        at, node, queues = _STATE.at, _STATE.node, _STATE.queues
    if at is None or clock() - at > stale_after or not node:
        return 0
    value = json.dumps({"node": node, "at": at})
    for queue in queues:
        client.set(presence_key(prefix, queue, node), value, ex=PRESENCE_TTL_SECONDS)
    return len(queues)


_STOP = threading.Event()


def start_presence(*, client_factory: Callable[[], Any] | None = None,
                   prefix: str | None = None) -> threading.Thread | None:
    """Called from `worker_ready` in the celery main process. Never raises.

    A thread of its own rather than a call from the beat, so a slow Redis can never stall the event
    loop whose health the beat reports."""
    try:
        if prefix is None:
            from meshpipeline.settings.providers import REDIS_KEY_PREFIX
            prefix = REDIS_KEY_PREFIX
        factory = client_factory or _default_client

        def _main() -> None:
            client = None
            while not _STOP.wait(PRESENCE_SECONDS):
                try:
                    if client is None:
                        client = factory()
                    publish_presence(client, prefix=prefix or "")
                except Exception as exc:  # noqa: BLE001 - presence is advisory; try again next time
                    logger.warning("worker liveness: could not publish presence (%s)",
                                   type(exc).__name__)
                    client = None

        thread = threading.Thread(target=_main, name="worker-presence", daemon=True)
        thread.start()
        return thread
    except Exception as exc:  # noqa: BLE001 - a worker that cannot report presence still works
        logger.warning("worker liveness: presence could not start (%s) - off", type(exc).__name__)
        return None


def _default_client() -> Any:
    from meshpipeline.adapters._shared.redis_client import sync_client
    return sync_client(socket_timeout=5, socket_connect_timeout=5)


# ---------------------------------------------------------------------------------------------
# the docker healthcheck
# ---------------------------------------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m meshpipeline.runtime.worker_liveness")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="exit 0 while the consumer beats, 1 once it has stopped")
    check.add_argument("--pid", type=int, default=1,
                       help="the worker's main process (PID 1 in a container)")
    check.add_argument("--stale-after", type=float, default=STALE_AFTER_SECONDS)
    args = parser.parse_args(argv)
    healthy, why = verdict(heartbeat_age(args.pid), args.stale_after)
    print(why)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["BEAT_SECONDS", "PRESENCE_KEY_TEMPLATE", "STALE_AFTER_SECONDS", "beat", "clear_heartbeat",
           "heartbeat_age", "heartbeat_path", "main", "presence_key", "publish_presence",
           "start_presence", "verdict"]
