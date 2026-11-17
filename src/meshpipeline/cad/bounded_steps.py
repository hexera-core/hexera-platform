# Responsibility: Run a list of CAD steps (a fuse, a Boolean check, a distance) in a child process, each under its own time limit, so one OpenCASCADE call that runs on and on costs its own step only - never the run.
# Boundaries: plumbing only. It decides nothing about the geometry: the caller gives the steps, their limits and what a step does (a handler in the caller's own module); this says which finished, with what answer, and which ran out of time or stopped. Never raises for a step's sake.
# Collaborates with: cad/assembly_join.py (the assembly's fuses, PLAN-fea-35), cad/suspect_defects.py (the Boolean checks, MESH-1).
"""BOUNDED STEPS.

An OpenCASCADE call (BOPAlgo_Builder.Perform, BRepAlgoAPI_Check.Perform) cannot be interrupted from Python, so a
step that must be time-boxed runs in a child process the parent can kill:

    results = run("meshpipeline.cad.assembly_join", payload, steps, budgets, total_s=...)

The child (`python -m <module> bounded-steps <spec> <dir> <first>`) reads the payload once (its shapes), then runs
the steps in order from `first`, writing `<k>.start` as step k starts and `<k>.json` (its answer) when it ends. The
parent watches those files: a step past its own limit is stopped (the child killed with its process group) and a
fresh child goes on from the next step, so the steps after it still run. A child that dies (a crash, the kernel's
memory killer) costs the step it was on. Past `total_s` nothing more is started: the steps left are "not run".

Each result: Step(index, ok, value, seconds, why) - `why` is "" (finished), "error" (the step raised: its words in
`value`), "time" (past its limit), "stopped" (the child died there) or "not run".
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: A fresh child's start (Python, OpenCASCADE, reading the payload's shapes) is not counted against its first
#: step; a child that has not started its step after this long is taken as stopped.
START_S = 180.0
#: How often the parent looks at the child's files, seconds.
POLL_S = 0.1
FINISHED, ERROR, TIME, STOPPED, NOT_RUN = "", "error", "time", "stopped", "not run"
ARG = "bounded-steps"


@dataclass
class Step:
    index: int
    ok: bool = False
    value: Any = None
    seconds: float = 0.0
    why: str = NOT_RUN


def run(module: str, payload: dict, steps: list[dict], budgets: list[float], *, total_s: float | None = None,
        start_by_s: float | None = None, workdir: Path | str | None = None) -> list[Step]:
    """Run `steps` in children of `python -m module` (see the module text); one Step per step, in order. A step's
    files (anything its handler writes into the work folder, e.g. a result shape `<k>.brep`) stay in `workdir`
    when one is given (the caller reads them); with none, a temporary folder that is removed after.
    `total_s`: past it the step running is stopped and nothing more runs. `start_by_s`: past it no step STARTS -
    the one running still has its own limit."""
    if not steps:
        return []
    if workdir is None:
        with tempfile.TemporaryDirectory(prefix="bounded_") as tmp:
            return run(module, payload, steps, budgets, total_s=total_s, start_by_s=start_by_s, workdir=tmp)
    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    spec = work / "spec.json"
    spec.write_text(json.dumps({"payload": payload, "steps": steps}, default=_plain))
    out = [Step(index=k) for k in range(len(steps))]
    t0 = time.monotonic()
    k = 0
    while k < len(steps):
        if total_s is not None and time.monotonic() - t0 >= float(total_s):
            break
        if start_by_s is not None and time.monotonic() - t0 >= float(start_by_s):
            break
        k = _one_child(module, spec, work, k, steps, budgets, out, t0, total_s, start_by_s)
    return out


def _one_child(module: str, spec: Path, work: Path, first: int, steps: list[dict], budgets: list[float],
               out: list[Step], t0: float, total_s: float | None, start_by_s: float | None = None) -> int:
    """One child from step `first`; returns the next step to start a child at (len(steps) when all are done)."""
    err = work / f"child{first}.err"
    try:
        with open(err, "wb") as fh:
            # stderr to a file, never a pipe nobody reads while the child runs (a full pipe would block it)
            proc = subprocess.Popen([sys.executable, "-m", module, ARG, str(spec), str(work), str(first)],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=fh,
                                    start_new_session=True)
    except OSError as exc:
        logger.warning("bounded steps: the child did not start (%s)", exc)
        for s in out[first:]:
            s.why, s.value = STOPPED, f"the child did not start ({type(exc).__name__})"
        return len(steps)
    spawned = time.monotonic()
    current, started = first, None
    try:
        while True:
            done = work / f"{current}.json"
            if done.exists():
                try:
                    got = json.loads(done.read_text())
                except ValueError:
                    got = None     # half-written: read again on the next look
                if got is not None:
                    s = out[current]
                    # the step's own time, from the child's two files (a quick step can start and end between
                    # two looks: timed from the parent's looks it counted the child's start and the steps before)
                    s.seconds = round(_took(work, current, time.monotonic() - (started or spawned)), 2)
                    s.ok = not got.get("error")
                    s.value = got.get("value") if s.ok else got.get("error")
                    s.why = FINISHED if s.ok else ERROR
                    current, started = current + 1, None
                    if current >= len(steps):
                        return current
                    if start_by_s is not None and time.monotonic() - t0 >= float(start_by_s):
                        # past the time to start another: the next is not run (its child stopped here)
                        return len(steps)
                    continue
            if started is None and (work / f"{current}.start").exists():
                started = time.monotonic()
            now = time.monotonic()
            if proc.poll() is not None:
                # the child is gone and the step it was on has no answer: a crash, or the memory killer
                if not (work / f"{current}.json").exists():
                    s = out[current]
                    s.seconds = round(now - (started or spawned), 2)
                    s.why, s.value = STOPPED, _stderr_tail(proc, err)
                    return current + 1
                continue
            if started is not None and now - started > float(budgets[current]):
                out[current].seconds, out[current].why = round(now - started, 2), TIME
                return current + 1
            if started is None and now - spawned > START_S:
                out[current].why, out[current].value = STOPPED, "the child did not start its step"
                return current + 1
            if total_s is not None and now - t0 > float(total_s):
                if started is not None:
                    out[current].seconds, out[current].why = round(now - started, 2), TIME
                return len(steps)
            time.sleep(POLL_S)
    finally:
        _kill(proc)


def _took(work: Path, k: int, fallback: float) -> float:
    """Step k's time from its start file to its answer file (both the child's), else `fallback`."""
    try:
        return max(0.0, (work / f"{k}.json").stat().st_mtime - (work / f"{k}.start").stat().st_mtime)
    except OSError:
        return fallback


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            proc.kill()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:  # pragma: no cover - a killed process that will not be reaped
        pass


def _stderr_tail(proc: subprocess.Popen, path: Path) -> str:
    try:
        text = path.read_bytes()[-4000:].decode(errors="replace")
    except OSError:
        text = ""
    tail = text.strip().splitlines()[-1:] or [f"exit {proc.returncode}"]
    return tail[0][:200]


def _plain(v):
    try:
        import numpy as np

        if isinstance(v, np.generic):
            return v.item()
        if isinstance(v, np.ndarray):
            return v.tolist()
    except ImportError:  # pragma: no cover
        pass
    return str(v)


def child_main(argv: list[str], load: Callable[[dict], Any], handle: Callable[[Any, dict, Path, int], Any]) -> int:
    """The child's side: `argv` = [spec, workdir, first]. `load(payload)` once (e.g. the shapes), then
    `handle(loaded, step, workdir, k)` for each step from `first`, its answer written as `<k>.json`."""
    _die_with_parent()
    spec_path, work, first = Path(argv[0]), Path(argv[1]), int(argv[2])
    spec = json.loads(spec_path.read_text())
    loaded = load(spec["payload"])
    for k in range(first, len(spec["steps"])):
        (work / f"{k}.start").write_text("")
        try:
            got = {"value": handle(loaded, spec["steps"][k], work, k)}
        except Exception as exc:  # noqa: BLE001 - the step's own failure, said as its answer
            got = {"error": f"{type(exc).__name__}: {exc}"[:300] or "error"}
        tmp = work / f"{k}.json.tmp"
        tmp.write_text(json.dumps(got, default=_plain))
        tmp.replace(work / f"{k}.json")
    return 0


def _die_with_parent() -> None:
    """The child is killed with its parent (Linux PR_SET_PDEATHSIG): a worker stopped mid-run leaves no fuse running
    on (lab, 17 Nov: a killed planner's touch check went on alone)."""
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, signal.SIGKILL)          # PR_SET_PDEATHSIG = 1
        if os.getppid() == 1:                  # the parent already gone before the signal was set
            os._exit(3)
    except (OSError, AttributeError):          # another system: the parent's own kill still stops it
        pass


def is_child(argv: list[str]) -> bool:
    return len(argv) > 1 and argv[1] == ARG


__all__ = ["ERROR", "FINISHED", "NOT_RUN", "STOPPED", "Step", "TIME", "child_main", "is_child", "run"]
