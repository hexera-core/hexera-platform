# Responsibility: TIME-21 - a big FEA sizing (PLAN-fea-22) runs on a machine of its own: how long the sizing would take on the geometry worker, predicted from what the check measured; above the line it is sent to the big-machine VM route (contracts/fea_sizing_remote) and the worker's queue slot is freed; a watch looks at it until it lands, and stores it on the check exactly as the worker would have; anything that goes wrong sizes it on the worker as before. Every VM it starts is released when the sizing ends, fails or is given up.
# Boundaries: decides where the sizing runs and stores what came back; the sizing itself is engines/fea_plan_options (the same code wherever it runs) and moving it is the remote's. Never raises into the check.
# Collaborates with: application/geometry_check.py (size_fea_plan calls send_away; store_plan_fact), application/fea_check.py (sizing_now reads `where`), contracts/fea_sizing_remote.py, adapters/pipeline_execution/celery.py (the watch task), application/pipeline_run.py (at_run_start).
"""TIME-21: A BIG SIZING ON A MACHINE OF ITS OWN.

THE PREDICTION, before the sizing starts, from the check's own measurement (cad/fea_measure via geometry_check):

    predicted worker seconds = SECONDS_PER_UNIT x (hot spots + surface triangles / TRIANGLES_PER_UNIT)

Each hot spot is a zone group the counts measure every cube against, and the surface is what the signed distance of
every cube is measured to (fea_size_field). Fitted on the lab's fixtures (4 cores, docs/RULES.md TIME-21) and the two
times measured on the site's 4-vCPU worker: a knuckle (one body, tens of hot spots) about 20 s, a 3-body customer
assembly with 1,764 hot spots and 95k triangles about 1,012 s. Its job is the big end: every part that took more than
a few minutes had several hundred hot spots or several hundred thousand triangles, and every part under one minute
had neither. A record with no triangle count (measured before it was kept) is predicted from its hot spots.

THE LINE: predicted over THRESHOLD_S on the worker. A VM costs about 90 s to start, its image pull and the hand-back
about as much again, and the sizing there is about three times faster (8 counting processes on whole cores against
4 vCPUs): it wins past about 4 minutes, and it frees the worker's slot besides.
"""
from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

RULE = "TIME-21"
#: the fit (module text, docs/RULES.md TIME-21)
SECONDS_PER_UNIT = 0.56
TRIANGLES_PER_UNIT = 2000.0
#: predicted worker seconds over this: the sizing runs on a VM
THRESHOLD_S = 240.0
#: of the check's sizing clock (fea_plan_options.BUDGET_S), what the VM's boot, image pull and hand-back are given:
#: the sizing there stops itself by the rest, so its own "out of time" lands before the marker's deadline
VM_ALLOWANCE_S = 300.0
#: how often the watch looks (each look is one exchange read; it holds a queue slot for a moment only)
WATCH_EVERY_S = 20
#: the marker's `where` while the sizing is away from the naming task: on a VM, or on the watch (the worker, after
#: the VM failed it). The naming task has ended then, so only the marker's clock settles it (fea_check.sizing_now)
ON_VM, AFTER_VM = "vm", "after_vm"
#: the run started before the VM's sizing landed: it sizes the mesh itself, as it always has (PLAN-fea-22)
RUN_FIRST = "The run started before the sizing ended"


def predicted_worker_s(record: dict | None) -> float | None:
    """How long the sizing would take on the 4-vCPU worker, in seconds (module text); None for a part that was not
    measured (nothing to go on: it is sized where it always was)."""
    if not isinstance(record, dict) or not record.get("measured"):
        return None
    spots = len(record.get("hot_spots") or [])
    tris = record.get("surface_triangles")
    work = float(spots) + (float(tris) / TRIANGLES_PER_UNIT if isinstance(tris, (int, float)) else 0.0)
    return round(SECONDS_PER_UNIT * work, 1)


def why_record(record: dict | None, *, where: str, machine: str = "", seconds: float | None = None,
               why: str = "") -> dict:
    """What the stored size choice says about where it was sized and why (TIME-21) - the same keys either way."""
    p = predicted_worker_s(record)
    out: dict = {"rule": RULE, "where": where, "predicted_worker_s": p, "threshold_s": THRESHOLD_S,
                 "hot_spots": len((record or {}).get("hot_spots") or []),
                 "surface_triangles": (record or {}).get("surface_triangles")}
    if machine:
        out["machine"] = machine
    if seconds is not None:
        out["seconds"] = round(float(seconds), 1)
    if why:
        out["why"] = why
    return out


def _remote():
    from meshpipeline.contracts import fea_sizing_remote as seam
    return seam.sizing_remote()


def send_away(session_id: str, src: Path, *, to_metres: float, budget_s: float, token: str, record: dict | None,
              interpretation: dict | None) -> bool:
    """TIME-21: the sizing of the canonical file `src` handed to a VM when its predicted worker time is over the line
    and the route is there: packed (fea_plan_options.pack), started (the speed machine, then its fallback), marked on
    the check (`where`: "vm", the VM's handle) and its watch queued. True once all of that is done - the caller then
    returns and its queue slot is free. False whenever any of it is not: nothing is left running, and the caller
    sizes it itself as before. Never raises."""
    from meshpipeline.contracts import fea_sizing_remote as seam

    p = predicted_worker_s(record)
    if p is None or p <= THRESHOLD_S:
        return False
    remote = _remote()
    if remote is None or not seam.can_watch():
        logger.info("fea sizing: predicted %.0f s on the worker, but no big-machine route here - sized on the worker "
                    "(%s) - session=%s", p, RULE, session_id)
        return False
    vm_budget = max(60.0, float(budget_s) - VM_ALLOWANCE_S)
    try:
        from meshpipeline.engines import fea_plan_options as PO
        with tempfile.TemporaryDirectory(prefix="fea_sizing_away_") as tmp:
            ws = PO.pack(Path(src), Path(tmp) / "ws", to_metres=to_metres, budget_s=vm_budget)
            handle = remote.start(ws, key=f"{session_id}|{token}", timeout_s=int(vm_budget + 120))
    except seam.SizingNotStarted as exc:
        logger.warning("fea sizing: predicted %.0f s on the worker; no VM could be had (%s) - sized on the worker "
                       "(%s) - session=%s", p, str(exc)[:300], RULE, session_id)
        return False
    except Exception as exc:  # noqa: BLE001 - nothing started (start leaves nothing behind): the worker sizes it
        logger.warning("fea sizing: not sent to a VM (%s: %s) - sized on the worker - session=%s",
                       type(exc).__name__, str(exc)[:200], session_id)
        return False
    handle = {**handle, "predicted_worker_s": p}
    from meshpipeline.application import geometry_check as gc
    if not gc.store_plan_fact(session_id, token, lambda mark: {**mark, "where": ON_VM, "vm": handle}):
        remote.release(handle, why="the check moved on before it was recorded")
        return False
    if not seam.enqueue_watch(session_id=session_id, token=token, handle=handle, interpretation=interpretation,
                              countdown=WATCH_EVERY_S):
        remote.release(handle, why="its watch could not be queued")
        gc.store_plan_fact(session_id, token,
                           lambda mark: {k: v for k, v in mark.items() if k not in ("where", "vm")})
        return False
    logger.warning("fea sizing: predicted %.0f s on the worker (over %.0f s) - sizing on a %s VM; the worker's slot "
                   "is free (%s) - session=%s", p, THRESHOLD_S, handle.get("machine"), RULE, session_id)
    return True


def watch(session_id: str, token: str, handle: dict, interpretation: dict | None = None, *,
          now: float | None = None) -> str:
    """ONE LOOK at a sizing on a VM (the watch task's body): what became of it, in a word. Stored on the check as the
    worker would have stored it when it landed; the VM released whenever the sizing ends, fails, or is given up
    (the check moved on - a new naming, a new unit, the session gone; the run started first; the marker's clock
    past); queued again while it is still at work; sized on the worker, as before, when the VM failed it. Never
    raises."""
    from meshpipeline.application import geometry_check as gc
    from meshpipeline.application.fea_check import PLAN_FACT_KEY
    from meshpipeline.contracts import fea_sizing_remote as seam

    now = time.time() if now is None else now
    remote = _remote()
    if remote is None:
        logger.warning("fea sizing watch: no big-machine route in this process - session=%s", session_id)
        return "no route"
    try:
        current = gc.read_check(session_id)
        mark = ((current or {}).get("facts") or {}).get(PLAN_FACT_KEY)
        if (not current or current.get("status") != gc.STATUS_READY or not isinstance(mark, dict)
                or mark.get("token") != token or not mark.get("sizing") or mark.get("where") != ON_VM):
            remote.release(handle, why="the check moved on (a new naming or unit, or the session is gone)")
            return "moved on"
        try:
            until = float(mark.get("until") or 0.0)
        except (TypeError, ValueError):
            until = 0.0
        if until and now > until:
            from meshpipeline.application.fea_check import out_of_time_words
            remote.release(handle, why="past the sizing's clock")
            gc.store_plan_fact(session_id, token, lambda _m: {
                "sized": False, "why": out_of_time_words(), "stopped": "clock",
                "sized_on": why_record(_record(current), where=ON_VM, machine=str(handle.get("machine") or ""))})
            return "out of time"
        seen = remote.look(handle)
        state = seen.get("state")
        if state == "running":
            if seam.enqueue_watch(session_id=session_id, token=token, handle=handle, interpretation=interpretation,
                                  countdown=WATCH_EVERY_S):
                return "waiting"
            remote.release(handle, why="its next look could not be queued")
            return _on_the_worker(session_id, token, interpretation, "its watch could not be queued")
        if state == "done":
            return _landed(session_id, token, handle, interpretation, current)
        remote.release(handle, why=str(seen.get("why") or "it was lost"))
        return _on_the_worker(session_id, token, interpretation, str(seen.get("why") or "its VM was lost"))
    except Exception as exc:  # noqa: BLE001 - a look that fails gives the sizing back to the worker
        logger.exception("fea sizing watch failed - session=%s", session_id)
        remote.release(handle, why=f"its watch failed ({type(exc).__name__})")
        return _on_the_worker(session_id, token, interpretation, f"its watch failed ({type(exc).__name__})")


def _record(current: dict | None) -> dict | None:
    from meshpipeline.cad.fea_measure import FACT_KEY
    got = ((current or {}).get("facts") or {}).get(FACT_KEY)
    return got if isinstance(got, dict) else None


def _landed(session_id: str, token: str, handle: dict, interpretation: dict | None, current: dict) -> str:
    """The VM's result, stored as the worker would have stored it: the same size choice on the check, the same
    plans beside it (the run reuses them). Only a result made by THIS code is used (fea_plan_options.planning_code):
    a mesh image on other code sizes by other rules, and its answer is never served as this worker's."""
    from meshpipeline.application import geometry_check as gc
    from meshpipeline.application.fea_check import out_of_time_words
    from meshpipeline.engines import fea_plan_options as PO

    remote = _remote()
    assert remote is not None
    machine = str(handle.get("machine") or "")
    seconds = time.time() - float(handle.get("started_at") or time.time())
    with tempfile.TemporaryDirectory(prefix="fea_sizing_back_") as tmp:
        dest = Path(tmp) / "ws"
        try:
            result = remote.collect(handle, dest)
        except Exception as exc:  # noqa: BLE001 - not brought back: the worker sizes it
            remote.release(handle, why=f"its result could not be brought back ({type(exc).__name__})")
            return _on_the_worker(session_id, token, interpretation,
                                  f"its result could not be brought back ({type(exc).__name__})")
        got = result.get("fea_sizing") if isinstance(result.get("fea_sizing"), dict) else {}
        same_code = got.get("planning_code") == PO.planning_code()
        options = got.get("options")
        plans = dest / PO.PLANS_FILE
        rec = why_record(_record(current), where=ON_VM, machine=machine, seconds=seconds)
        if same_code and result.get("rc") == 0 and isinstance(options, dict) and "full" in options \
                and plans.is_file():
            from meshpipeline.contracts.object_storage import get_object_store
            get_object_store().upload_file(local_path=plans, object_key=gc.check_object_key(session_id, PO.PLANS_FILE))
            stored = gc.store_plan_fact(session_id, token, lambda _m: {"sized": True, **options, "sized_on": rec})
            remote.release(handle, why="its result was collected")
            logger.warning("fea sizing on a %s VM: sized in %.0f s (predicted %.0f s on the worker, %s) - session=%s",
                           machine, seconds, float(handle.get("predicted_worker_s") or 0.0), RULE, session_id)
            return "sized" if stored else "moved on"
        if same_code and got.get("out_of_time"):
            # TIME-11: stopped by its own clock - the worker, three times slower, would have been stopped too
            remote.release(handle, why="its sizing ran out of time")
            gc.store_plan_fact(session_id, token, lambda _m: {"sized": False, "why": out_of_time_words(),
                                                              "stopped": "clock", "sized_on": rec})
            return "out of time"
    if not got:
        # a mesh image from before TIME-21 answers "unknown engine": nothing was sized there
        why = (f"its mesh image could not size it (rc {result.get('rc')}: "
               f"{str(result.get('log_tail') or '')[:200]})")
    elif not same_code:
        why = "its mesh image sizes by other code"
    else:
        why = f"it was not sized there ({str(got.get('error') or 'no size choice')[:200]})"
    remote.release(handle, why=why)
    return _on_the_worker(session_id, token, interpretation, why)


def _on_the_worker(session_id: str, token: str, interpretation: dict | None, why: str) -> str:
    """THE SIZING AS BEFORE: the VM failed it, so it is sized here, in this task, with its whole clock (TIME-11: the
    marker's deadline starts again, and says it is no longer the naming's - that task has ended)."""
    from meshpipeline.application import geometry_check as gc

    logger.warning("fea sizing: the VM did not size it (%s) - sizing on the worker as before (%s) - session=%s",
                   why, RULE, session_id)
    at = time.time()
    if not gc.store_plan_fact(session_id, token, lambda mark: {
            **{k: v for k, v in mark.items() if k != "vm"}, "where": AFTER_VM, "vm_why": why[:300],
            "until": round(at + gc.sizing_deadline_s(), 1)}):
        return "moved on"
    current = gc.read_check(session_id) or {}
    got = gc.size_fea_plan(session_id, current, interpretation, token=token, started=at, record=_record(current),
                           away=False, why=why)
    return "sized on the worker" if got.get("sized") else "not sized on the worker"


def at_run_start(session_id: str, job_id: str = "") -> None:
    """The run has started (application/pipeline_run): a sizing still on a VM is given up - the run sizes the mesh
    itself and takes the recommended one, as it always has when it starts first - and its VM released; the size
    choice it starts from is cited in its why record (TIME-21: where it was sized and why). Never raises."""
    if not session_id:
        return
    try:
        from meshpipeline.application import geometry_check as gc
        from meshpipeline.application.fea_check import PLAN_FACT_KEY
        current = gc.read_check(session_id)
        mark = ((current or {}).get("facts") or {}).get(PLAN_FACT_KEY)
        if not isinstance(mark, dict):
            return
        on = mark.get("sized_on")
        if mark.get("sizing") and mark.get("where") == ON_VM and isinstance(mark.get("vm"), dict):
            vm = mark["vm"]
            remote = _remote()
            if remote is not None:
                remote.release(vm, why="the run started first")
            on = why_record(_record(current), where=ON_VM, machine=str(vm.get("machine") or ""),
                            why="the run started first")
            gc.store_plan_fact(session_id, str(mark.get("token") or ""), lambda _m: {
                "sized": False, "why": RUN_FIRST, "stopped": "run", "sized_on": on})
            logger.warning("fea sizing: the run started first - its VM released (%s) - session=%s", RULE, session_id)
        if job_id and isinstance(on, dict):
            from meshpipeline.contracts import decision_record as dr
            where = str(on.get("where") or "worker")
            dr.cite(job_id, stage="fea_sizing", decided="where", value=where, rule=RULE,
                    inputs={k: on.get(k) for k in ("predicted_worker_s", "threshold_s", "hot_spots",
                                                     "surface_triangles", "machine", "seconds", "why")},
                    alternatives=["worker", ON_VM],
                    words=(f"The mesh was being sized on a {on.get('machine')} VM when the run started: the run sized "
                           "it itself." if where == ON_VM and on.get("why") == "the run started first" else
                           f"The mesh was sized on a {on.get('machine')} VM: the geometry worker would have taken "
                           f"about {on.get('predicted_worker_s')} s, over the {on.get('threshold_s')} s line."
                           if where == ON_VM and on.get("machine") else
                           f"The mesh was sized on the geometry worker (predicted {on.get('predicted_worker_s')} s"
                           f" there; the line for a VM is {on.get('threshold_s')} s)."))
    except Exception:  # noqa: BLE001 - the run goes on whatever this did
        logger.exception("fea sizing: the run's start did not settle its sizing - session=%s", session_id)


__all__ = ["AFTER_VM", "ON_VM", "RULE", "RUN_FIRST", "SECONDS_PER_UNIT", "THRESHOLD_S", "TRIANGLES_PER_UNIT",
           "VM_ALLOWANCE_S", "WATCH_EVERY_S", "at_run_start", "predicted_worker_s", "send_away", "watch",
           "why_record"]
