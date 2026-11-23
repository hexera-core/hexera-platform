# Responsibility: Run every v4 kernel call in a warm, spawned worker process that can be SIGKILLed.
# Owns: the worker loop, the pool, the per-call time limit, kill-and-respawn on timeout or crash.
# Boundaries: knows no geometry. It moves JSON-serialisable dicts; shapes are BREP file paths in
#             them. Which functions exist is `kernel_calls.REGISTRY`.
"""A pool of warm kernel workers, spawned (never forked) and killed when a call overruns.

OCCT can hang or segfault in ways a Python-level timeout cannot interrupt, so the only bound that
holds is a process boundary: a call that overruns its limit gets its worker SIGKILLed and replaced.
Workers are spawned because forking a process that has threads (or has loaded OCCT) is unsafe.

OCP is imported once per worker at start so calls are warm. The import is guarded: on a host
without OCP (the hermetic unit tier) the worker still starts and runs the non-OCCT functions.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import queue
import signal
import time
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import Literal

logger = logging.getLogger(__name__)

Status = Literal["ok", "error", "timeout", "crashed"]


@dataclass(frozen=True)
class CallResult:
    ok: bool
    value: dict | None
    error: str | None
    status: Status
    seconds: float


def _worker_main(conn: Connection) -> None:
    """Worker entry point: warm the kernel, say ready, then serve calls until told to stop."""
    try:
        import OCP  # noqa: F401  # warm start; absent on hosts without OCCT
    except ImportError:
        pass
    from meshpipeline.cad.repair_v4 import kernel_calls

    conn.send(("ready", os.getpid()))
    while True:
        try:
            msg = conn.recv()
        except EOFError:
            return
        if msg is None:
            return
        fn_name, payload = msg
        fn = kernel_calls.REGISTRY.get(fn_name)
        if fn is None:
            conn.send(("error", f"unknown kernel function {fn_name!r}"))
            continue
        try:
            conn.send(("ok", fn(payload)))
        except Exception as exc:  # a raising kernel call is a result, not a dead worker
            conn.send(("error", f"{type(exc).__name__}: {exc}"))


class _Worker:
    def __init__(self, process: BaseProcess, conn: Connection) -> None:
        self.process = process
        self.conn = conn

    def kill(self) -> None:
        pid = self.process.pid
        if pid is not None and self.process.is_alive():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.process.join(5)
        self.conn.close()


class WorkerPool:
    """`size` warm workers; `call` runs one registered function in an idle one."""

    def __init__(self, size: int, *, start_timeout_s: float = 30) -> None:
        if size < 1:
            raise ValueError("size must be at least 1")
        self._ctx = multiprocessing.get_context("spawn")
        self._start_timeout_s = start_timeout_s
        self._idle: queue.Queue[_Worker] = queue.Queue()
        self._all: list[_Worker] = []
        self._closed = False
        for _ in range(size):
            self._idle.put(self._spawn())

    @property
    def start_method(self) -> str:
        return self._ctx.get_start_method()

    def _spawn(self) -> _Worker:
        parent_conn, child_conn = self._ctx.Pipe()
        process = self._ctx.Process(target=_worker_main, args=(child_conn,), daemon=True)
        process.start()
        child_conn.close()
        worker = _Worker(process, parent_conn)
        self._all.append(worker)
        if not parent_conn.poll(self._start_timeout_s):
            worker.kill()
            self._all.remove(worker)
            raise RuntimeError(f"kernel worker did not start within {self._start_timeout_s}s")
        parent_conn.recv()  # ("ready", pid)
        return worker

    def _replace(self, worker: _Worker) -> _Worker:
        worker.kill()
        self._all.remove(worker)
        return self._spawn()

    def call(self, fn_name: str, payload: dict, *, timeout_s: float) -> CallResult:
        if self._closed:
            raise RuntimeError("pool is closed")
        worker = self._idle.get()
        start = time.monotonic()
        status: Status
        value: dict | None = None
        error: str | None = None
        try:
            worker.conn.send((fn_name, payload))
            if not worker.conn.poll(timeout_s):
                status, error = "timeout", f"{fn_name} exceeded {timeout_s}s; worker killed"
                worker = self._replace(worker)
            else:
                try:
                    kind, body = worker.conn.recv()
                except (EOFError, OSError):
                    status, error = "crashed", f"worker died during {fn_name}"
                    worker = self._replace(worker)
                else:
                    if kind == "ok":
                        status, value = "ok", body
                    else:
                        status, error = "error", body
        except (BrokenPipeError, OSError):
            status, error = "crashed", f"worker died before {fn_name}"
            worker = self._replace(worker)
        finally:
            self._idle.put(worker)
        seconds = time.monotonic() - start
        if status != "ok":
            logger.warning("kernel call %s: %s (%s)", fn_name, status, error)
        return CallResult(ok=status == "ok", value=value, error=error, status=status, seconds=seconds)

    def close(self) -> None:
        self._closed = True
        for worker in list(self._all):
            try:
                worker.conn.send(None)
            except (BrokenPipeError, OSError):
                pass
            worker.process.join(2)
            worker.kill()
        self._all.clear()
