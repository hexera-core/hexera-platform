# Responsibility: Pin that the v4/v5 kernel pool starts inside a DAEMONIC process - a Celery prefork child, where the
#                 product runs the repair - instead of failing "daemonic processes are not allowed to have children"
#                 (measured on the crsv5ship fleet: every v5 run fell back to v3 on it).
from __future__ import annotations

import multiprocessing

from meshpipeline.cad.repair_v4.worker_pool import _children_allowed


def test_the_daemon_flag_is_lifted_for_the_start_only():
    config = multiprocessing.current_process()._config  # type: ignore[attr-defined]
    before = config.get("daemon")
    config["daemon"] = True
    try:
        with _children_allowed():
            assert not config.get("daemon")
        assert config["daemon"] is True
    finally:
        if before is None:
            config.pop("daemon", None)
        else:
            config["daemon"] = before


def _child(q):  # pragma: no cover - runs in the spawned process
    q.put("ok")


def test_a_daemonic_parent_can_start_a_child_inside_the_window():
    ctx = multiprocessing.get_context("spawn")
    config = multiprocessing.current_process()._config  # type: ignore[attr-defined]
    before = config.get("daemon")
    config["daemon"] = True
    try:
        q = ctx.Queue()
        p = ctx.Process(target=_child, args=(q,), daemon=True)
        with _children_allowed():
            p.start()
        assert q.get(timeout=60) == "ok"
        p.join(timeout=30)
    finally:
        if before is None:
            config.pop("daemon", None)
        else:
            config["daemon"] = before
