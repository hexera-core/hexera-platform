# Responsibility: CRS v5's STREAMING event log - one JSON object per line, written the moment it happens, so a file
#                 the wall kills, the OOM killer takes or a worker crash ends still says where it was and why.
# Owns: `emit` / `failure` / `enter` (phase transitions) / `file_start` / `file_end`, the process context (run, file,
#       round, defect, phase), the heartbeat thread and the SIGTERM net.
# Boundaries: knows nothing about geometry. Off unless `CRS_V5_EVENT_LOG` names a file (or CRS_V5_EVENT_STDOUT=1);
#             when off every call is a cheap no-op. Never raises into the caller: a log that cannot be written is
#             dropped, never a failed repair. The query side is devtools/validation/v4_eval/events_query.py.
"""The CRS v5 event log.

WHERE IT GOES. `CRS_V5_EVENT_LOG=<path>` appends one JSON line per event to `<path>` with ONE `os.write` on an
O_APPEND descriptor (so the pool's worker processes, which inherit the variable, append to the same file without
tearing a line) and fsyncs it at a file's end and at every failure. The same line goes to stdout (unless
`CRS_V5_EVENT_STDOUT=0`) with a `severity` field, which Docker's gcplogs driver ships to Cloud Logging.

WHAT A LINE CARRIES. `ts`, `seq` (per process), `pid`, `event`, `severity`, `elapsed_s` (process) and `file_elapsed_s`,
the RUN context from the environment (`CRS_V5_RUN_ID`, `CRS_V5_COMMIT`, `CRS_V5_VM`, `CRS_V5_SHARD`), the FILE context
`file_start` set (sha256, name, arm, part faces/solids) and the moving context (`phase`, `round`, `defect_*`, `op`,
`tier`), then the event's own fields. A failure adds `exc_type`, `exc_message` and `traceback` (the last ~20 lines).

THE PROCESS NET. A heartbeat every `CRS_V5_HEARTBEAT_S` (30 s) says the phase, the op, the elapsed time and the RSS
of this process and of its children (the pool workers and their mesher children), so an OOM-killed file still shows
where it was and who was big. SIGTERM writes a `file_end` (reason `signal`) before the process dies.
"""

from __future__ import annotations

import atexit
import datetime as _dt
import json
import math
import os
import signal
import sys
import threading
import time
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from meshpipeline.settings.env import optional_env

ENV_PATH = "CRS_V5_EVENT_LOG"
ENV_STDOUT = "CRS_V5_EVENT_STDOUT"
ENV_HEARTBEAT = "CRS_V5_HEARTBEAT_S"
#: run-level labels, set per unit by the VM run script
RUN_ENV = {"run_id": "CRS_V5_RUN_ID", "commit": "CRS_V5_COMMIT", "vm": "CRS_V5_VM", "shard": "CRS_V5_SHARD"}

#: a string field is kept to this many characters (head and tail when longer: an error's end is the useful part)
MAX_STR = 4000
#: a whole line is kept under this; the largest fields are dropped (named) until it fits. Cloud Logging takes 256 KB
MAX_LINE = 60_000
TRACEBACK_LINES = 20
DEFAULT_HEARTBEAT_S = 30.0

#: keys an event's own fields may not overwrite (they are renamed `x_<key>`)
CORE = ("ts", "seq", "pid", "event", "severity", "elapsed_s", "file_elapsed_s")

#: `set_context(**NO_DEFECT)` / `enter(..., **NO_DEFECT)`: no defect is in hand any more
NO_DEFECT: dict[str, Any] = dict.fromkeys(("defect_id", "defect_class", "defect_code", "defect_kind", "defect_member"))

#: the keys `enter` moves in the context (its other fields are the phase event's own)
MOVING_KEYS = frozenset(("round", "op", "tier", *NO_DEFECT))

_lock = threading.RLock()
_state: dict[str, Any] = {"fd": None, "fd_pid": None, "path": None, "seq": 0, "t0": time.monotonic(),
                          "file_t0": None, "phase_t0": None, "ctx": {}, "hb": None, "hb_stop": None,
                          "started": False, "sig_installed": False}


# --------------------------------------------------------------------------- switches


def log_path() -> str | None:
    return optional_env("CRS_V5_EVENT_LOG", "") or None


def to_stdout() -> bool:
    flag = optional_env("CRS_V5_EVENT_STDOUT", "")
    if flag.strip():
        return flag.strip().lower() not in ("0", "false", "no", "off")
    return log_path() is not None


def _run_labels() -> dict[str, str]:
    """The run-level labels (`RUN_ENV`), each read by its literal name so the settings catalogue sees every one."""
    found = {"run_id": optional_env("CRS_V5_RUN_ID", ""), "commit": optional_env("CRS_V5_COMMIT", ""),
             "vm": optional_env("CRS_V5_VM", ""), "shard": optional_env("CRS_V5_SHARD", "")}
    return {k: v for k, v in found.items() if v}


#: in-process listeners (`add_listener`): each gets every event's record, log on or off. The product path uses one
#: to turn the event stream into plain progress lines for the job (`repair_v5.result.ProgressMapper`).
_listeners: list[Any] = []


def add_listener(fn: Any) -> None:
    """Call `fn(record)` for every later event of this process (the record `emit` would write). Never raises into
    the run: a listener that fails is skipped for that event."""
    with _lock:
        _listeners.append(fn)


def remove_listener(fn: Any) -> None:
    with _lock:
        try:
            _listeners.remove(fn)
        except ValueError:
            pass


def _active() -> bool:
    return bool(_listeners) or enabled()


def enabled() -> bool:
    return log_path() is not None or to_stdout()


# --------------------------------------------------------------------------- values


def _clip(text: str, limit: int = MAX_STR) -> str:
    if len(text) <= limit:
        return text
    head = limit // 3
    return f"{text[:head]} ...[{len(text) - limit} chars cut]... {text[-(limit - head):]}"


def jsonable(value: Any, depth: int = 0) -> Any:
    """JSON-safe: tuples as lists, non-finite floats as strings, long strings clipped, unknown objects as `repr`."""
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return _clip(value)
    if depth > 8:
        return _clip(repr(value), 400)
    if isinstance(value, dict):
        return {str(k): jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        seq = list(value)
        out = [jsonable(v, depth + 1) for v in seq[:200]]
        if len(seq) > 200:
            out.append(f"...[{len(seq) - 200} more]")
        return out
    return _clip(repr(value), 1000)


def _encode(rec: dict) -> str:
    line = json.dumps(rec, separators=(",", ":"), default=str)
    if len(line) <= MAX_LINE:
        return line
    dropped: list[str] = []
    for key in sorted((k for k in rec if k not in CORE), key=lambda k: -len(json.dumps(rec[k], default=str))):
        dropped.append(key)
        rec[key] = f"<dropped: {len(json.dumps(rec[key], default=str))} chars>"
        line = json.dumps({**rec, "dropped_fields": dropped}, separators=(",", ":"), default=str)
        if len(line) <= MAX_LINE:
            return line
    return line[:MAX_LINE]


# --------------------------------------------------------------------------- memory


def _status_kb(pid: int | str, key: str) -> int | None:
    try:
        with open(f"/proc/{pid}/status", encoding="ascii", errors="replace") as fh:
            for row in fh:
                if row.startswith(key + ":"):
                    return int(row.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _children(pid: int) -> list[int]:
    out: list[int] = []
    try:
        for tid in os.listdir(f"/proc/{pid}/task"):
            try:
                with open(f"/proc/{pid}/task/{tid}/children", encoding="ascii") as fh:
                    out.extend(int(c) for c in fh.read().split())
            except OSError:
                continue
    except OSError:
        return []
    return out


def memory() -> dict[str, Any]:
    """RSS and high-water mark of this process (MB), and of its process tree (Linux /proc; elsewhere only the
    high-water mark from getrusage). Cheap: a few small /proc reads."""
    pid = os.getpid()
    rss = _status_kb(pid, "VmRSS")
    hwm = _status_kb(pid, "VmHWM")
    if rss is None:
        try:
            import resource

            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            hwm = peak // 1024 if sys.platform == "darwin" else peak
        except Exception:  # noqa: BLE001
            return {}
        return {"rss_hwm_mb": round(hwm / 1024, 1)}
    tree, biggest, todo = 0, None, _children(pid)
    seen: set[int] = set()
    while todo and len(seen) < 256:
        c = todo.pop()
        if c in seen:
            continue
        seen.add(c)
        kb = _status_kb(c, "VmRSS") or 0
        tree += kb
        if biggest is None or kb > biggest[1]:
            biggest = (c, kb)
        todo.extend(_children(c))
    out: dict[str, Any] = {"rss_mb": round(rss / 1024, 1), "rss_hwm_mb": round((hwm or 0) / 1024, 1),
                           "children": len(seen), "children_rss_mb": round(tree / 1024, 1)}
    if biggest is not None:
        try:
            with open(f"/proc/{biggest[0]}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            cmd = ""
        out["biggest_child"] = {"pid": biggest[0], "rss_mb": round(biggest[1] / 1024, 1), "cmd": cmd[:200]}
    return out


# --------------------------------------------------------------------------- writing


def _fd() -> int | None:
    path = log_path()
    if path is None:
        return None
    if _state["fd"] is not None and _state["fd_pid"] == os.getpid() and _state["path"] == path:
        return _state["fd"]
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o666)
    except OSError:
        return None
    _state.update(fd=fd, fd_pid=os.getpid(), path=path)
    return fd


def _write(line: str, *, sync: bool) -> None:
    data = (line + "\n").encode("utf-8", "replace")
    fd = _fd()
    if fd is not None:
        try:
            os.write(fd, data)
            if sync:
                os.fsync(fd)
        except OSError:
            pass
    if to_stdout():
        try:
            os.write(1, data)
        except OSError:
            pass


def _now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def emit(event: str, /, *, severity: str = "INFO", sync: bool = False, **fields: Any) -> None:
    """One event, written now (and handed to every listener). Never raises."""
    if not _active():
        return
    try:
        with _lock:
            _state["seq"] += 1
            now = time.monotonic()
            rec: dict[str, Any] = {"ts": _now_iso(), "seq": _state["seq"], "pid": os.getpid(), "event": event,
                                   "severity": severity, "elapsed_s": round(now - _state["t0"], 3)}
            if _state["file_t0"] is not None:
                rec["file_elapsed_s"] = round(now - _state["file_t0"], 3)
            rec.update(_run_labels())
            rec.update(_state["ctx"])
            for k, v in fields.items():
                rec[f"x_{k}" if k in CORE else k] = jsonable(v)
            if severity in ("ERROR", "CRITICAL"):
                sync = True
            listeners = list(_listeners)
        for listener in listeners:
            try:
                listener(rec)
            except Exception:  # noqa: BLE001 - a listener never breaks the run
                pass
        if enabled():
            _write(_encode(rec), sync=sync)
    except Exception:  # noqa: BLE001 - the log never breaks the run
        pass


def exception_fields(exc: BaseException) -> dict[str, Any]:
    lines = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip().splitlines()
    return {"exc_type": type(exc).__name__, "exc_message": _clip(str(exc), 2000),
            "traceback": "\n".join(lines[-TRACEBACK_LINES:])}


def failure(event: str, exc: BaseException | None = None, /, *, severity: str = "ERROR", **fields: Any) -> None:
    """A failure event: the exception's type, message and the tail of its traceback, plus memory; fsynced."""
    extra = exception_fields(exc) if exc is not None else {}
    emit(event, severity=severity, sync=True, **extra, **memory(), **fields)


# --------------------------------------------------------------------------- context


def set_context(**ctx: Any) -> None:
    """Merge into the process context every later event carries (None removes a key)."""
    with _lock:
        for k, v in ctx.items():
            if v is None:
                _state["ctx"].pop(k, None)
            else:
                _state["ctx"][k] = jsonable(v)


def context() -> dict[str, Any]:
    with _lock:
        return dict(_state["ctx"])


def adopt(ctx: dict[str, Any] | None) -> None:
    """A pool worker takes the caller's context for the call it is about to run."""
    if not ctx:
        return
    with _lock:
        _state["ctx"] = dict(ctx)


def defect_fields(defect: Any) -> dict[str, Any]:
    if defect is None:
        return {}
    return {"defect_id": getattr(defect, "id", None), "defect_class": getattr(defect, "cls", None),
            "defect_code": getattr(defect, "code", None), "defect_kind": getattr(defect, "kind", None),
            "defect_member": getattr(defect, "member", None)}


def heat_done(run: Any, defect: Any) -> Any:
    """A tournament heat's event (its `Match` row and the defect), as the heat finishes; returns `run` unchanged."""
    if enabled():
        match = getattr(run, "match", None)
        row = match.row() if match is not None and hasattr(match, "row") else {}
        emit("heat", severity="INFO" if row.get("result") == "survived" else "WARNING",
             **{**defect_fields(defect), **row})
    return run


def wall_stop(where: str, why: Any, defect: Any = None, **fields: Any) -> None:
    """A stop on the file's (or the class loop's) wall, at `where`."""
    reason = f"{type(why).__name__}: {why}" if isinstance(why, BaseException) else str(why)
    emit("wall_stop", severity="WARNING", sync=True, where=where, reason_key="wall", reason=reason,
         **defect_fields(defect), **memory(), **fields)


def enter(phase: str, **fields: Any) -> None:
    """A phase transition: the context says `phase` from now on, and the event names the phase it ends and how long
    that ran (`prev_phase`, `prev_wall_s`), so per-phase wall time is a sum over these events. Of `fields`, the round,
    op, tier and defect keys move the context; the rest are this event's own."""
    if not _active():
        return
    with _lock:
        now = time.monotonic()
        prev = _state["ctx"].get("phase")
        prev_s = round(now - _state["phase_t0"], 3) if _state["phase_t0"] is not None else None
        _state["phase_t0"] = now
    set_context(phase=phase, **{k: v for k, v in fields.items() if k in MOVING_KEYS})
    emit("phase", prev_phase=prev, prev_wall_s=prev_s, **{k: v for k, v in fields.items() if k not in MOVING_KEYS})


@contextmanager
def op(name: str, **ctx: Any) -> Iterator[None]:
    """The context says `op` = `name` inside the block (the heartbeat's "current op")."""
    prev = context().get("op")
    set_context(op=name, **ctx)
    try:
        yield
    finally:
        set_context(op=prev)


# --------------------------------------------------------------------------- the file


def file_start(sha256: str, name: str, **ctx: Any) -> None:
    if not enabled():
        return
    startup()
    with _lock:
        _state["ctx"] = {}          # the context is the file's: nothing of the last file carries over
        _state["file_t0"] = _state["phase_t0"] = time.monotonic()
    set_context(file_sha256=sha256, file_name=name, **ctx)
    emit("file_start", sync=True, **memory())


def file_end(result: str, *, reason: str = "", severity: str = "INFO", **fields: Any) -> None:
    """The file's last event (fsynced), closing its open phase; the file context is cleared after it."""
    if not enabled():
        return
    with _lock:
        if _state["file_t0"] is None:
            return
        now = time.monotonic()
        last = _state["ctx"].get("phase")
        last_s = round(now - _state["phase_t0"], 3) if _state["phase_t0"] is not None else None
    emit("file_end", severity=severity, sync=True, result=result, reason=reason, prev_phase=last,
         prev_wall_s=last_s, wall_s=round(now - _state["file_t0"], 3), **memory(), **fields)
    with _lock:
        _state["file_t0"] = _state["phase_t0"] = None
        _state["ctx"] = {}


def file_open() -> bool:
    return _state["file_t0"] is not None


# --------------------------------------------------------------------------- the process net


def _heartbeat_loop(stop: threading.Event, every: float) -> None:
    while not stop.wait(every):
        left = None
        try:
            from meshpipeline.cad.repair_v4.verdict import wall

            left = wall.remaining_s()
        except Exception:  # noqa: BLE001
            pass
        emit("heartbeat", sync=True, file_wall_left_s=None if left is None else round(left, 1),
             threads=threading.active_count(), **memory())


def start_heartbeat(every: float | None = None) -> None:
    if not enabled() or _state["hb"] is not None:
        return
    every = float(optional_env("CRS_V5_HEARTBEAT_S", "") or (every or DEFAULT_HEARTBEAT_S))
    if every <= 0:
        return
    stop = threading.Event()
    th = threading.Thread(target=_heartbeat_loop, args=(stop, every), name="crs-v5-heartbeat", daemon=True)
    _state.update(hb=th, hb_stop=stop)
    th.start()


def stop_heartbeat() -> None:
    stop = _state.get("hb_stop")
    if stop is not None:
        stop.set()
    _state.update(hb=None, hb_stop=None)


def _on_signal(signum: int, frame: Any) -> None:  # noqa: ARG001
    name = signal.Signals(signum).name
    failure("signal", severity="CRITICAL", signal=name)
    if file_open():
        file_end("killed", reason=f"signal: {name}", severity="CRITICAL")
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def _at_exit() -> None:
    if file_open():
        file_end("abandoned", reason="process_exit: the process ended with the file open", severity="ERROR")
    emit("process_end", sync=True, **memory())


def startup(**fields: Any) -> None:
    """Once per process: a `process_start` event, the heartbeat, the exit hook and (main thread only) SIGTERM's
    net. Called by the harness at start-up and, lazily, by the first `file_start`."""
    if not enabled() or _state["started"]:
        return
    _state["started"] = True
    mem_total = None
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            mem_total = round(int(fh.readline().split()[1]) / 1024)
    except (OSError, ValueError, IndexError):
        pass
    emit("process_start", sync=True, argv=sys.argv[:40], python=sys.version.split()[0], cpus=os.cpu_count(),
         mem_total_mb=mem_total, log=log_path(), **fields)
    start_heartbeat()
    atexit.register(_at_exit)
    if threading.current_thread() is threading.main_thread() and not _state["sig_installed"]:
        try:
            if signal.getsignal(signal.SIGTERM) in (signal.SIG_DFL, None):
                signal.signal(signal.SIGTERM, _on_signal)
                _state["sig_installed"] = True
        except (ValueError, OSError):
            pass


def _reset_for_tests() -> None:
    stop_heartbeat()
    with _lock:
        if _state["fd"] is not None and _state["fd_pid"] == os.getpid():
            try:
                os.close(_state["fd"])
            except OSError:
                pass
        _state.update(fd=None, fd_pid=None, path=None, seq=0, t0=time.monotonic(), file_t0=None, phase_t0=None,
                      ctx={}, started=False)


__all__ = ["adopt", "context", "defect_fields", "emit", "enabled", "enter", "exception_fields", "failure",
           "file_end", "file_open", "file_start", "jsonable", "memory", "op", "set_context", "start_heartbeat",
           "startup", "stop_heartbeat"]
