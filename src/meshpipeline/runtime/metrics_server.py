# Responsibility: Serve the worker's aggregated metrics, and keep the multiprocess registry clean across forks.
# Boundaries: stale per-pid files are cleared at start, so a restarted worker never reports a dead fork's numbers.
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

#: The worker metrics endpoint. Fixed: the stack maps this port, nothing chooses it per
#: deployment, and a developer who changed it would only hide the endpoint from the stack.
WORKER_METRICS_PORT = 9100


def _mp_dir() -> str | None:
    from meshpipeline.settings.env import optional_env
    return optional_env("PROMETHEUS_MULTIPROC_DIR", "") or None


def reset_multiproc_dir() -> None:
    d = _mp_dir()
    if not d:
        return
    p = Path(d)
    p.mkdir(parents=True, exist_ok=True)
    for f in p.glob("*.db"):
        try:
            f.unlink()
        except OSError:
            pass


def start_worker_metrics_server() -> None:
    if not _mp_dir():
        logger.info("worker metrics: PROMETHEUS_MULTIPROC_DIR unset - exporter disabled")
        return
    try:
        from prometheus_client import CollectorRegistry, multiprocess, start_http_server
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        port = WORKER_METRICS_PORT
        start_http_server(port, registry=registry)
        logger.info("worker metrics: exporter on :%d (multiprocess aggregated)", port)
    except Exception as exc:
        logger.warning("worker metrics: failed to start exporter: %s", exc)


def mark_process_dead() -> None:
    if not _mp_dir():
        return
    try:
        from prometheus_client import multiprocess
        multiprocess.mark_process_dead(os.getpid())
    except Exception:
        pass
