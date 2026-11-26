# Responsibility: The CRS v5 PRODUCT entry point - STEP in, STEP out - for `cad/repair/conservative.repair_step_file`.
# Owns: `repair_file`, `V5Run`, `V5Unavailable`.
# Boundaries: no OCCT here: every kernel call goes through `WorkerPool.call`. It composes the shipped pieces in the
#             order the evaluation's v5 arm does (devtools/validation/v4_eval/arms.py `_v5_arm`, the reference): read,
#             size the part, judge the input, staged healer + confirmation, `loop.repair_v5` with the v4 AI search as
#             its broader repair, the delivery gate and catastrophic-loss guard, then write. It never decides whether a
#             job should be repaired, never uploads, never records an attempt.
# Collaborates with: repair_v5/result.py (the contract), repair_v5/events.py (progress), settings/cad_repair.py.
"""CRS v5 on the product path."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from meshpipeline.cad.repair_v5.result import ProgressMapper, RepairResultV5, from_v5

#: the AI proposer's model rounds per file in the broader repair (as measured: run_arm.py `--max-turns` default)
MAX_TURNS = 12
#: the broader repair's applied-op budget (evalspec.Budget.ops, as measured)
SEARCH_OPS = 60
READ_TIMEOUT_S = 300.0
WRITE_TIMEOUT_S = 120.0
#: seconds of the file wall kept back for the STEP write and the teardown
WRITE_RESERVE_S = 30.0


class V5Unavailable(RuntimeError):
    """v5 could not run or could not measure this file (pool, reader, checker, forced failure). OURS, never a verdict on
    the part: the caller falls back to v3."""


@dataclass
class V5Run:
    """One v5 file. `delivered`: the repaired STEP is at the destination; otherwise the upload's bytes are."""

    delivered: bool
    result: RepairResultV5
    #: the full technical trace for admin views: the readable lines, match counts, per-phase numbers, spend split
    trace: dict[str, Any] = field(default_factory=dict)
    #: every plain progress event the run produced, in order
    progress: list[dict[str, Any]] = field(default_factory=list)


def _workers() -> int:
    return max(1, min(4, os.cpu_count() or 1))


def _route() -> str:
    from meshpipeline.contracts.model_inference import REPAIR_V4_PROPOSE_ROUTES

    return REPAIR_V4_PROPOSE_ROUTES[0]


def _preflight() -> float:
    """The run's model cap, or `V5Unavailable` (the verification hook, or a cap above the hard ceiling)."""
    import meshpipeline.settings.cad_repair as rcfg
    from meshpipeline.cad.repair_v4.proposer import FILE_USD_CEILING

    if rcfg.v5_force_fail():
        raise V5Unavailable("CRS_V5_FORCE_FAIL is set: v5 refused this run on purpose (verification hook)")
    cap = float(rcfg.CRS_V5_FILE_USD_CAP)
    if not (0 < cap <= FILE_USD_CEILING):
        raise V5Unavailable(f"CRS_V5_FILE_USD_CAP={cap!r} is outside (0, {FILE_USD_CEILING}]")
    return cap


def _daemonic() -> bool:
    import multiprocessing

    return bool(multiprocessing.current_process().daemon)


def repair_file(source: str | Path, dest: str | Path, *, deadline_s: float, choice: str = "meshing",
                auto_accept_mm: float | None = None, on_progress: Callable[[dict[str, Any]], None] | None = None,
                clock: Callable[[], float] = time.monotonic) -> V5Run:
    """Repair the STEP at `source` into `dest` with CRS v5 before the monotonic `deadline_s`. Raises `V5Unavailable`
    when it could not measure the file (the caller runs v3 instead); any other exception is a v5 bug and is also the
    caller's cue to run v3. On a return, `dest` holds either the repaired STEP or the upload's bytes.

    WHERE IT RUNS. A daemonic caller - the product's: the repair node runs inside a Celery prefork child - cannot start
    the kernel pool (measured on the crsv5ship fleet: "daemonic processes are not allowed to have children", and with
    that flag lifted, spawn refuses to pickle the billiard child's authkey). So from a daemonic process the run is its
    own child process (`runtime/crs_v5_child.py`), its progress read live from the v5 event log; otherwise it runs
    here."""
    _preflight()
    if _daemonic():
        return _repair_in_child(source, dest, deadline_s=deadline_s, choice=choice, auto_accept_mm=auto_accept_mm,
                                on_progress=on_progress, clock=clock)
    return repair_in_process(source, dest, deadline_s=deadline_s, choice=choice, auto_accept_mm=auto_accept_mm,
                             on_progress=on_progress, clock=clock)


#: seconds past the run's own deadline before the child is killed (it stops itself at the deadline; this is the net)
CHILD_GRACE_S = 120.0


def _repair_in_child(source: str | Path, dest: str | Path, *, deadline_s: float, choice: str,
                     auto_accept_mm: float | None, on_progress: Callable[[dict[str, Any]], None] | None,
                     clock: Callable[[], float]) -> V5Run:
    import json
    import os
    import signal
    import subprocess
    import sys

    from meshpipeline.cad.repair_v5 import events as EV
    from meshpipeline.cad.repair_v5.result import RepairResultV5

    mapper = ProgressMapper()
    with tempfile.TemporaryDirectory(prefix="v5-child-") as work:
        result_path = os.path.join(work, "result.json")
        events_path = EV.log_path() or os.path.join(work, "events.jsonl")
        offset = os.path.getsize(events_path) if os.path.exists(events_path) else 0
        env = dict(os.environ, CRS_V5_EVENT_LOG=events_path)
        env.setdefault("CRS_V5_EVENT_STDOUT", "0")
        left = max(1.0, deadline_s - clock())
        cmd = [sys.executable, "-m", "meshpipeline.runtime.crs_v5_child", str(source), str(dest), result_path,
               f"{left:.1f}", choice, "none" if auto_accept_mm is None else str(auto_accept_mm)]
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                start_new_session=True)

        def drain() -> None:
            nonlocal offset
            if not os.path.exists(events_path):
                return
            with open(events_path, "rb") as fh:
                fh.seek(offset)
                chunk = fh.read()
            cut = chunk.rfind(b"\n") + 1
            offset += cut
            for line in chunk[:cut].splitlines():
                try:
                    got = mapper(json.loads(line))
                except ValueError:
                    continue
                if got is not None and on_progress is not None:
                    try:
                        on_progress(got)
                    except Exception:  # noqa: BLE001 - a progress line never breaks the run
                        pass

        killed = False
        while proc.poll() is None:
            drain()
            if clock() > deadline_s + CHILD_GRACE_S:
                os.killpg(proc.pid, signal.SIGKILL)
                killed = True
                break
            time.sleep(1.0)
        proc.wait()
        drain()
        stderr = (proc.stderr.read() if proc.stderr is not None else b"").decode("utf-8", "replace")[-1500:]
        if killed:
            raise V5Unavailable(f"the v5 run outlived its deadline by {CHILD_GRACE_S:.0f} s and was stopped")
        try:
            with open(result_path, encoding="utf-8") as fh:
                out = json.load(fh)
        except (OSError, ValueError) as exc:
            raise V5Unavailable(f"the v5 child left no result (exit {proc.returncode}): {stderr.strip()[-500:]}") \
                from exc
    if "error" in out:
        raise V5Unavailable(f"the v5 child failed: {out['error']}")
    return V5Run(delivered=bool(out["delivered"]), result=RepairResultV5.from_dict(out["result"]),
                 trace=dict(out.get("trace") or {}), progress=list(out.get("progress") or mapper.seen))


def repair_in_process(source: str | Path, dest: str | Path, *, deadline_s: float, choice: str = "meshing",
                      auto_accept_mm: float | None = None,
                      on_progress: Callable[[dict[str, Any]], None] | None = None,
                      clock: Callable[[], float] = time.monotonic) -> V5Run:
    """`repair_file` in THIS process (it must be allowed to start the kernel pool's worker processes)."""
    from meshpipeline.cad.repair_v4.verdict import wall
    from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
    from meshpipeline.cad.repair_v5 import events as EV

    cap = _preflight()
    source, dest = str(source), str(dest)
    digest = hashlib.sha256(Path(source).read_bytes()).hexdigest()
    mapper = ProgressMapper()

    def listen(rec: dict[str, Any]) -> None:
        got = mapper(rec)
        if got is not None and on_progress is not None:
            on_progress(got)

    EV.add_listener(listen)
    EV.file_start(digest, Path(source).name, arm="v5_product")
    wall.set_file_deadline(deadline_s - WRITE_RESERVE_S)
    t0 = clock()
    try:
        try:
            pool = WorkerPool(_workers())
        except Exception as exc:  # noqa: BLE001 - no pool is our failure, whatever its type
            raise V5Unavailable(f"the worker pool could not start: {type(exc).__name__}: {exc}") from exc
        try:
            with tempfile.TemporaryDirectory(prefix="v5-run-") as work:
                run = _run(pool, source, dest, work, digest=digest, deadline_s=deadline_s, cap=cap, choice=choice,
                           auto_accept_mm=auto_accept_mm, clock=clock, t0=t0)
        finally:
            pool.close()
        EV.emit("v5_done", fixed=run.result.fixed, total=len(run.result.problems), outcome=run.result.outcome)
        EV.file_end("delivered" if run.delivered else "passthrough", outcome=run.result.outcome)
        run.progress = list(mapper.seen)
        return run
    except BaseException as exc:
        EV.failure("v5_failed", exc)
        EV.file_end("error", reason=f"{type(exc).__name__}", severity="ERROR")
        raise
    finally:
        wall.set_file_deadline(None)
        EV.remove_listener(listen)


def _run(pool: Any, source: str, dest: str, work: str, *, digest: str, deadline_s: float, cap: float, choice: str,
         auto_accept_mm: float | None, clock: Callable[[], float], t0: float) -> V5Run:
    from types import SimpleNamespace

    import meshpipeline.settings.cad_repair as rcfg
    from meshpipeline.cad.repair_v4 import search as S
    from meshpipeline.cad.repair_v4.limits import budget_scale
    from meshpipeline.cad.repair_v4.loss import check_loss
    from meshpipeline.cad.repair_v4.model_ledger import WaveLedger
    from meshpipeline.cad.repair_v4.stages import confirm_staged, run_staged
    from meshpipeline.cad.repair_v4.trace import Trace
    from meshpipeline.cad.repair_v4.verdict import wall
    from meshpipeline.cad.repair_v4.verdict.assemble import (
        InconclusiveVerdict,
        adaptive_cheap_members,
        set_evidence_order,
    )
    from meshpipeline.cad.repair_v4.verdict.valid import CheckerUnavailable
    from meshpipeline.cad.repair_v4.workspace import Workspace
    from meshpipeline.cad.repair_v5 import events as EV
    from meshpipeline.cad.repair_v5 import tournament as T
    from meshpipeline.cad.repair_v5.agent_entrants import AgentSource
    from meshpipeline.cad.repair_v5.describe_ai import FileDescriber
    from meshpipeline.cad.repair_v5.loop import DELIVER_RESERVE_FACTOR, HEAL_SHARE, repair_v5

    EV.emit("v5_start")
    EV.enter("read")
    read = pool.call("read_step_xcaf", {"path": source, "brep_out": str(Path(work) / "input.brep")},
                     timeout_s=max(1.0, min(READ_TIMEOUT_S, deadline_s - clock())))
    if not read.ok or read.value is None:
        raise V5Unavailable(f"the STEP reader could not read the file ({read.status}: {read.error})")
    doc = read.value
    try:
        ws = Workspace.from_brep(pool, doc["brep"], doc.get("labels") or {})
    except RuntimeError as exc:
        raise V5Unavailable(f"the part could not be indexed: {exc}") from exc
    names = [str(sid) for sid in (getattr(ws, "ids", None) or ())]
    faces, solids = sum(n.startswith("F") for n in names), sum(n.startswith("B") for n in names)
    scale = budget_scale(faces or None, solids or None)
    wall.set_member_scale(scale)
    EV.set_context(part_faces=faces, part_solids=solids, budget_scale=scale)
    try:
        h_mm = S.h_mm_for(pool, ws)
        judge = S.default_judge(h_mm=h_mm)
        EV.enter("input_verdict")
        t_in = clock()
        v_in = judge.full(pool, ws, source)
        took_in = clock() - t_in
        if getattr(v_in, "inconclusive", False):
            raise InconclusiveVerdict("the input could not be measured")
    except (CheckerUnavailable, InconclusiveVerdict) as exc:
        raise V5Unavailable(f"the checker could not measure the input ({type(exc).__name__}: {exc})") from exc
    set_evidence_order(v_in)
    trace = Trace()
    trace.record("start", arm="v5_product")
    base_trace: dict[str, Any] = {"engine": "v5", "h_mm": h_mm, "part_faces": faces, "part_solids": solids,
                                  "budget_scale": scale, "input_clean": bool(v_in.clean)}

    def passthrough(why: str) -> V5Run:
        shutil.copyfile(source, dest)
        result = from_v5(choice=choice, input_verdict=v_in, final_verdict=v_in, delivered=False, history=(),
                         matches=(), seconds=clock() - t0, spend_usd=0.0, auto_accept_mm=auto_accept_mm)
        return V5Run(delivered=False, result=result, trace={**base_trace, "why": why})

    try:
        if v_in.clean:
            return passthrough("the input is already clean: nothing to repair")
        file_wall = max(0.0, deadline_s - t0)
        if getattr(v_in, "out_of_scope", False):
            start = ws                    # an unbounded input skips the staged healer (repair_v4 lane C9)
        else:
            EV.enter("heal")
            staged = run_staged(pool, ws, cheap_members=adaptive_cheap_members(v_in), budget_s=HEAL_SHARE * file_wall)
            EV.enter("heal_confirm")
            confirm_staged(pool, ws, staged, full=lambda w: judge.full(pool, w, source), input_verdict=v_in)
            start = staged.ws
        route = _route()
        ledger = WaveLedger.for_file(cap)
        job_id = f"repair_v5:{digest[:12]}"
        searches: list[dict[str, Any]] = []

        def fallback(best: Any, _verdict: Any, left_s: float) -> Any:
            generators, proposer = S.arm_generators(
                "search_ai", route=route, ledger=ledger, max_turns=MAX_TURNS, usd_ceiling=cap, file_id=digest,
                job_id=job_id)
            sbudget = S.Budget(wall_s=left_s, ops=SEARCH_OPS, model_calls=MAX_TURNS, model_usd=cap)
            result = S.search(pool, best, source, generators, sbudget, workers=_workers(), judge=judge, h_mm=h_mm,
                              clock=clock)
            searches.append({"reason": getattr(result, "reason", None), "clean": bool(getattr(result, "clean", False)),
                             "model_unavailable": bool(proposer is not None and proposer.model_unavailable)})
            return result

        agent = (AgentSource(pool=pool, ledger=ledger, file_id=digest, route=route, job_id=job_id, file_cap_usd=cap,
                             trace=trace) if rcfg.CRS_V5_AGENT else None)
        describe = FileDescriber(ledger, digest, route, cap)
        outcome = repair_v5(pool, start, source, judge=judge, sites_of=judge.sites, h_mm=h_mm,
                            wall_s=float(rcfg.CRS_V5_SEARCH_WALL_S) * scale,
                            fallback=fallback if rcfg.CRS_V5_FALLBACK else None, clock=clock, trace=trace,
                            sources=(*T.DEFAULT_SOURCES, *((agent,) if agent is not None else ())),
                            ai_describe=describe, reserve_s=DELIVER_RESERVE_FACTOR * took_in)
        changed = outcome.best.brep_path != ws.brep_path
        EV.enter("deliver", changed=changed, **EV.NO_DEFECT)
        loss = check_loss(pool, ws.brep_path, outcome.best.brep_path) if changed else None
        stand_in: Any = SimpleNamespace(changed=changed, best=outcome.best, best_verdict=outcome.verdict)
        delivered = S.deliverable(stand_in, v_in, loss=loss)
        out_v = v_in if delivered is None else judge.full(pool, delivered, source)
        if delivered is not None:
            staging = str(Path(work) / "out.step")
            written = pool.call("write_step_xcaf",
                                {"brep": delivered.brep_path, "labels": delivered.labels,
                                 "units": doc.get("units", "mm"), "assembly_ids": doc.get("assembly_ids", []),
                                 "path": staging},
                                timeout_s=max(1.0, min(WRITE_TIMEOUT_S, deadline_s - clock())))
            if not written.ok:
                raise V5Unavailable(f"the repaired STEP could not be written ({written.status}: {written.error})")
            shutil.move(staging, dest)
        else:
            shutil.copyfile(source, dest)
        spend = float(ledger.file_spent_usd(digest))
        result = from_v5(choice=choice, input_verdict=v_in, final_verdict=out_v, delivered=delivered is not None,
                         history=outcome.seq.counters.history, matches=outcome.matches.records,
                         seconds=clock() - t0, spend_usd=spend, auto_accept_mm=auto_accept_mm)
        admin = {**base_trace,
                 "lines": outcome.lines()[:400],
                 "matches": outcome.matches.counts(),
                 "counters": outcome.seq.counters.row(),
                 "fallback": {"ran": outcome.fallback is not None, "why": outcome.fallback_why,
                              "improved": bool(outcome.fallback_improved), "searches": searches},
                 "loss": loss.row() if loss is not None else None,
                 "spend_usd": {"file": round(spend, 5), "cap": cap,
                               "agent": round(agent.spent_usd, 5) if agent is not None else None,
                               "describe": round(describe.spent_usd, 5)},
                 "output_clean": bool(getattr(out_v, "clean", False))}
        return V5Run(delivered=delivered is not None, result=result, trace=admin)
    except (CheckerUnavailable, InconclusiveVerdict) as exc:
        raise V5Unavailable(f"the checker could not run ({type(exc).__name__}: {exc})") from exc
    finally:
        set_evidence_order(None)


__all__ = ["V5Run", "V5Unavailable", "repair_file", "repair_in_process"]
