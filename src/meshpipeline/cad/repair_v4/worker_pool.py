# Responsibility: Run every v4 kernel call in a warm, spawned worker process that can be SIGKILLed.
# Owns: the worker loop, the pool, the per-call time limit, kill-and-respawn on timeout or crash.
# Boundaries: knows no geometry. It moves JSON-serialisable dicts; shapes are BREP file paths in
#             them. Which functions exist is `kernel_calls.REGISTRY`.
"""A pool of warm kernel workers, spawned (never forked) and killed when a call overruns.

OCCT can hang or segfault in ways a Python-level timeout cannot interrupt, so the only bound that
holds is a process boundary: a call that overruns its limit gets its worker SIGKILLed and replaced.
Workers are spawned because forking a process that has threads (or has loaded OCCT) is unsafe.

The channel is kept self-synchronising two ways. (1) A call that does not run to a reply - the pool's
own timeout, a worker death, or ANY exception escaping the call (a caller's SIGALRM file deadline, a
KeyboardInterrupt) - retires its worker, so a reply that was never read cannot be left in a channel
that will be reused. (2) Every request carries an id the reply must echo; a reply for any other id is
never returned as a result (it is a desynchronised channel: the call reports `crashed` and the worker
is replaced). (1) is why it does not happen; (2) is why, if it ever did, it would be loud rather than a
wrong verdict.

OCP is imported once per worker at start so calls are warm. The import is guarded: on a host
without OCP (the hermetic unit tier) the worker still starts and runs the non-OCCT functions.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import multiprocessing
import os
import queue
import signal
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import Any, Literal

from meshpipeline.cad.repair_v5 import events as EV

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
        req_id, fn_name, payload, *ctx = msg   # ctx: the caller's event-log context, when the log is on
        EV.adopt(ctx[0] if ctx else None)
        fn = kernel_calls.REGISTRY.get(fn_name)
        if fn is None:
            conn.send(("error", req_id, f"unknown kernel function {fn_name!r}"))
            continue
        try:
            conn.send(("ok", req_id, fn(payload)))
        except Exception as exc:  # a raising kernel call is a result, not a dead worker
            EV.failure("kernel_exception", exc, op=fn_name)   # the traceback the reply's one line cannot carry
            conn.send(("error", req_id, f"{type(exc).__name__}: {exc}"))


@contextlib.contextmanager
def _main_script_hidden() -> Iterator[None]:
    """Start a worker WITHOUT re-running the caller's script in it.

    A spawned child re-executes the parent's `__main__` file (multiprocessing's `init_main_from_path`) before its
    target runs, so a plain script that calls `repair_v4()` at module level, with no `if __name__ == "__main__":`
    guard, starts the repair again inside every worker and the pool dies with EOFError. A worker needs nothing from
    `__main__`: its target is `_worker_main` in this module and it moves only JSON-serialisable dicts. So while the
    child is being prepared, a script `__main__` (one with no module spec; `python -m` callers are imported by name
    and are left alone) hides its `__file__`, and the child starts from this module alone."""
    with _MAIN_LOCK:   # two pools spawning on two threads must not restore each other's hidden path early
        main = sys.modules.get("__main__")
        path = getattr(main, "__file__", None) if main is not None else None
        if main is None or path is None or getattr(main, "__spec__", None) is not None:
            yield
            return
        del main.__file__
        try:
            yield
        finally:
            main.__file__ = path


_MAIN_LOCK = threading.Lock()


class _Worker:
    def __init__(self, process: BaseProcess, conn: Connection) -> None:
        self.process = process
        self.conn = conn
        self.next_id = 0  # request ids; a reply must echo the id of the request it answers
        self.busy = False  # checked out by a call; that call, not close(), owns the connection

    def kill(self, *, close_conn: bool = True) -> None:
        pid = self.process.pid
        if pid is not None and self.process.is_alive():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.process.join(5)
        if close_conn:
            self.conn.close()


class Deadline:
    """A deadline that can be moved EARLIER while calls wait on it (the parallel panel's doom clock, D23): `at` is a
    time.monotonic() instant, or None for no deadline beyond each call's own timeout."""

    def __init__(self) -> None:
        self.at: float | None = None


#: the deadline a call made from this context must also honour (set per member thread by `panel.run_panel`)
CALL_DEADLINE: contextvars.ContextVar[Deadline | None] = contextvars.ContextVar("pool_call_deadline", default=None)
_POLL_SLICE_S = 0.5


def _poll_until(conn: Any, end: float) -> bool:
    """`conn.poll` until `end` or the context's `CALL_DEADLINE`, whichever is first; True when a reply is waiting.
    Without a context deadline this is one `conn.poll(end - now)`, exactly as before."""
    cut = CALL_DEADLINE.get()
    if cut is None:
        return bool(conn.poll(max(0.0, end - time.monotonic())))
    while True:
        now = time.monotonic()
        stop = end if cut.at is None else min(end, cut.at)
        if now >= stop:
            return bool(conn.poll(0))
        if conn.poll(min(_POLL_SLICE_S, stop - now)):
            return True


#: a successful kernel call at least this long is logged (the event log's "where did the wall go")
SLOW_CALL_S = 2.0


def _event(fn_name: str, status: str, error: str | None, start: float, timeout_s: float, worker: Any) -> None:
    """The call's event: every failure (timeout, crash, worker death, a raising kernel fn) with the worker's pid and
    exit code, and every slow success."""
    if not EV.enabled():
        return
    seconds = round(time.monotonic() - start, 3)
    if status == "ok" and seconds < SLOW_CALL_S:
        return
    proc = getattr(worker, "process", None)
    fields = {"op": fn_name, "kernel_call": fn_name, "status": status, "timeout_s": timeout_s, "wall_s": seconds,
              "worker_pid": getattr(proc, "pid", None), "worker_exit_code": getattr(proc, "exitcode", None)}
    if status == "ok":
        EV.emit("kernel_call", severity="DEBUG", **fields)  # type: ignore[arg-type]
    else:
        EV.emit("kernel_call_failed", severity="ERROR" if status == "crashed" else "WARNING", sync=True,
                error=error, reason_key=f"kernel_{status}", **fields, **EV.memory())


class WorkerPool:
    """`size` warm workers; `call` runs one registered function in an idle one.

    The idle queue holds a worker, or None for a slot whose worker could not be respawned; the next
    caller to take that slot tries to spawn again, so a failed respawn never poisons the pool.
    """

    def __init__(self, size: int, *, start_timeout_s: float = 30) -> None:
        if size < 1:
            raise ValueError("size must be at least 1")
        self._ctx = multiprocessing.get_context("spawn")
        self._start_timeout_s = start_timeout_s
        self._idle: queue.Queue[_Worker | None] = queue.Queue()
        self._all: list[_Worker] = []
        self._lock = threading.Lock()
        self._closed = False
        for _ in range(size):
            self._idle.put(self._spawn())

    @property
    def start_method(self) -> str:
        return self._ctx.get_start_method()

    def _spawn(self) -> _Worker:
        """Start one worker and wait for its ready message; on any failure leave nothing behind."""
        parent_conn, child_conn = self._ctx.Pipe()
        process = self._ctx.Process(target=_worker_main, args=(child_conn,), daemon=True)
        worker = _Worker(process, parent_conn)
        with self._lock:
            if self._closed:
                parent_conn.close()
                child_conn.close()
                raise RuntimeError("pool is closed")
            try:
                with _main_script_hidden():
                    process.start()
            except BaseException:
                parent_conn.close()
                child_conn.close()
                raise
            self._all.append(worker)
        child_conn.close()
        try:
            if not parent_conn.poll(self._start_timeout_s):
                raise RuntimeError(f"kernel worker did not start within {self._start_timeout_s}s")
            parent_conn.recv()  # ("ready", pid)
        except BaseException:
            self._discard(worker)
            raise
        return worker

    def _discard(self, worker: _Worker) -> None:
        worker.kill()
        with self._lock:
            if worker in self._all:
                self._all.remove(worker)

    def _replace(self, worker: _Worker) -> _Worker | None:
        """Kill `worker` and spawn its successor; None if the successor could not be started."""
        self._discard(worker)
        try:
            return self._spawn()
        except Exception as exc:
            logger.warning("kernel worker respawn failed: %s", exc)
            return None

    def call(self, fn_name: str, payload: dict, *, timeout_s: float) -> CallResult:
        if self._closed:
            raise RuntimeError("pool is closed")
        slot = self._idle.get()
        if slot is not None:
            with self._lock:
                slot.busy = True
        start = time.monotonic()
        status: Status
        value: dict | None = None
        error: str | None = None
        worker: _Worker | None = None
        try:
            if slot is None:
                try:
                    slot = self._spawn()
                    with self._lock:
                        slot.busy = True
                except Exception as exc:
                    slot = None
                    status, error = "crashed", f"no worker for {fn_name}: {exc}"
                    return self._done(fn_name, status, value, error, start)
            worker = slot
            clean = False  # True only once this call's own reply (or a recorded death) has been dealt with
            try:
                worker.next_id += 1
                req_id = worker.next_id
                worker.conn.send((req_id, fn_name, payload, EV.context()) if EV.enabled() else (req_id, fn_name, payload))
                if not _poll_until(worker.conn, start + timeout_s):
                    cut = CALL_DEADLINE.get()
                    if cut is not None and cut.at is not None and time.monotonic() < start + timeout_s:
                        status, error = "timeout", f"{fn_name} cut at the doomed cap; worker killed"
                    else:
                        status, error = "timeout", f"{fn_name} exceeded {timeout_s}s; worker killed"
                    slot = self._replace(worker)
                    clean = True
                else:
                    try:
                        kind, got_id, body = worker.conn.recv()
                    except (EOFError, OSError):
                        status, error = "crashed", f"worker died during {fn_name}"
                        slot = self._replace(worker)
                        clean = True
                    else:
                        if got_id != req_id:
                            # Never a result: a reply to some other request must not be taken as this one's.
                            status = "crashed"
                            error = f"{fn_name}: reply for request {got_id!r}, expected {req_id}; worker replaced"
                            slot = self._replace(worker)
                        elif kind == "ok":
                            status, value = "ok", body
                        else:
                            status, error = "error", body
                        clean = True
            except (BrokenPipeError, OSError):
                status, error = "crashed", f"worker died before {fn_name}"
                slot = self._replace(worker)
                clean = True
            finally:
                if not clean:
                    # An exception is leaving the call mid-flight (a caller's deadline signal, Ctrl-C): the
                    # worker may still be computing and WILL write a reply nobody reads. Retire it.
                    slot = self._replace(worker)
        finally:
            if slot is not None:
                slot.busy = False
            self._idle.put(slot)
        _event(fn_name, status, error, start, timeout_s, worker)
        return self._done(fn_name, status, value, error, start)

    @staticmethod
    def _done(fn_name: str, status: Status, value: dict | None, error: str | None, start: float) -> CallResult:
        if status != "ok":
            logger.warning("kernel call %s: %s (%s)", fn_name, status, error)
        return CallResult(
            ok=status == "ok",
            value=value,
            error=error,
            status=status,
            seconds=time.monotonic() - start,
        )

    def close(self) -> None:
        with self._lock:
            self._closed = True
            workers = list(self._all)
            self._all.clear()
        for worker in workers:
            if worker.busy:
                # Mid-call: kill it and let the call see EOF and close its own connection.
                worker.kill(close_conn=False)
                continue
            try:
                worker.conn.send(None)
            except (BrokenPipeError, OSError):
                pass
            worker.process.join(2)
            worker.kill()
