# Responsibility: Map an engine name to the callable that runs it in-process, and keep the run's
# progress channel moving across the phases of a mesh run that have no subprocess to watch.
# Boundaries: dispatch only; every runner it names owns its own native lifecycle.
# Collaborates with: the six engine runners and adapters/mesh_execution/local.py.
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

# OpenFOAM engines carry LLM-authored dicts that must be scanned before executing; the
# scan is engine-owned (each bundle carries its own foam_exec clone). gmsh/vmtk author no
# such dicts.
_OPENFOAM_ENGINES = frozenset({"cfmesh", "cfmesh_conjugate", "snappy",
                               "snappy_multiregion"})


#: THE SAME SIGNS OF LIFE, FOR A PHASE WITH NO SUBPROCESS TO WATCH.
#:
#: What "progress" MEANS on a mesh run is one thing and one thing only
#: (`runtime/mesh_runner._output_mark`): a file matching `*.log` or `*/*.log` under the workspace
#: whose size or mtime has moved. `_beacon` moves `progress_at` when that tuple changes, the
#: waiting worker reads `progress_at` off the beacon and calls `progress_clock.touch()`
#: (`adapters/mesh_execution/cloud_run_client._relay_progress`), and `application/pipeline_budget.
#: progress_extension` moves the run's deadline on from there - but only while the last touch is
#: inside PROGRESS_GRACE_S (1,020 s). A phase that writes no workspace log for longer than that is
#: a SILENT WINDOW: the job stops counting it as progress and the lease is lost under a run that is
#: working perfectly.
#:
#: The OpenFOAM stages have logs already (`cfmesh_conjugate/conjugate._foam` opens one per stage
#: and hands the handle to `run_guarded`, which is also how the stall watch resolves the file it
#: reads: `safe_exec._log_of`). What had no log at all was every phase that runs INSIDE this
#: process - the review surface, the boundary index, and the return this function performs below.
#: Measured on the Corintis plate's priced path, the two return measure passes alone are
#: (4.12 + 21.17) s/GiB x 590 GiB = 14,921 s of silence, fourteen times the grace.
#:
#: SO THE SIGNAL IS THE HOUSE ONE AND ONLY THE CHANNEL IS NEW. `sandbox/liveness.LivenessWatch` is
#: the repository's one authority on "is it still working" - its own words: "a run shows life in
#: any of three ways, and it is stalled only when all three have been flat: its OUTPUT moved, its
#: process tree burned CPU, its process tree's memory changed by a real amount". `safe_exec.
#: _wait_watched` polls exactly that for a subprocess and calls `progress_clock.touch()` on every
#: sign. `watching_progress` below polls exactly that for THIS process and writes one line to a
#: workspace log on every sign, so the signal reaches the remote channel as well as the local one.
#:
#: IT CANNOT FAKE A STUCK PHASE, which is the whole reason it is this and not a ticker. A phase
#: that has hung gives no sign: its outputs stop moving, it burns no CPU and its resident size
#: holds still, so `poll()` returns None, nothing is written, the log stops growing and the 900 s
#: stall window ends the run exactly as `contracts/native_time` says it must. The watch thread's
#: own cost is one `/proc` walk per look, which is three orders of magnitude under
#: `liveness.MIN_CPU_SHARE` (5% of a core over the interval), so it cannot register as its own
#: sign of life either.
WATCH_EVERY_S = 30.0
#: THE WATCH MUST NOT READ ITS OWN COST AS THE PHASE'S LIFE. `LivenessWatch` reads the CPU of the
#: whole process tree, and this watcher is a THREAD of that process rather than a child of it, so
#: it cannot be excluded the way `safe_exec` excludes its own: whatever the look itself burns is
#: inside the number it is reading. Measured 2026-11-12 in this container (102 processes in
#: /proc): `sample_tree` costs 1.24 ms of wall and 1.27 ms of CPU per call. At the 30 s interval
#: above, `liveness.MIN_CPU_SHARE` already puts the bar at 1.5 s of CPU - 1,180 times that - so in
#: production this cannot happen; this floor is what keeps it true at ANY interval a caller picks,
#: and 50 ms is 39 times the measured walk. It never binds at the production interval, where the
#: share is the larger of the two.
WATCH_MIN_CPU_S = 0.05
#: Where a watched phase's signs of life are written: `<phase>.log` beside the mesh, which is a
#: name `_output_mark` already globs and `engines/mesher_stage` reads as a tool it does not know
#: (so the live line keeps the last stage it said rather than being blanked).
PHASE_LOG = "{phase}.log"
#: The phase name the return below is watched under.
RETURN_PHASE = "return_home"


@contextmanager
def watching_progress(workspace: Any, *, phase: str, watch_paths: Any = (),
                      every_s: float | None = None) -> Iterator[None]:
    """While the body runs, relay THIS process's signs of life into `<workspace>/<phase>.log`.

    See WATCH_EVERY_S above for why this exists and why it cannot report a stuck phase as
    working. `watch_paths` are the files this phase writes, handed to `LivenessWatch` as its
    output sign the same way `safe_exec` hands it a stage log; CPU and memory are read off the
    process tree whether any path is given or not.

    Never raises and never fails a run: a log that cannot be written is a line that is not said.
    """
    import logging
    import os
    import threading
    import time
    from pathlib import Path

    from meshpipeline.contracts import progress_clock

    log = Path(workspace) / PHASE_LOG.format(phase=phase)
    every = WATCH_EVERY_S if every_s is None else max(0.001, float(every_s))
    stop = threading.Event()
    t0 = time.monotonic()

    def say(line: str) -> None:
        try:
            with log.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            logging.getLogger(__name__).debug("progress: %s not written", log, exc_info=True)
        # the local route runs the engine in the worker's own process, where this clock IS the
        # one pipeline_budget reads - the same touch safe_exec._wait_watched makes on every sign
        progress_clock.touch()

    def loop() -> None:
        from meshpipeline.sandbox.liveness import LivenessWatch
        alive = LivenessWatch(os.getpid(), tuple(str(p) for p in watch_paths),
                              min_cpu_s=WATCH_MIN_CPU_S)
        while not stop.wait(every):
            try:
                sign = alive.poll()
            except Exception:  # noqa: BLE001 - a look that fails is no sign either way
                sign = None
            if sign:
                say(f"{phase}: {sign} after {time.monotonic() - t0:.0f}s")

    say(f"{phase}: started")
    watcher = threading.Thread(target=loop, name=f"progress-{phase}", daemon=True)
    watcher.start()
    try:
        yield
    finally:
        stop.set()
        watcher.join(timeout=every + 5.0)
        say(f"{phase}: finished after {time.monotonic() - t0:.0f}s")


def engine_runners() -> dict:
    from meshpipeline.engines.cfmesh.native import _run_cartesian_mesh_local
    from meshpipeline.engines.cfmesh_conjugate.native import _run_conjugate_local
    from meshpipeline.engines.gmsh.gmsh_runner import _run_gmsh_local
    from meshpipeline.engines.snapgrid.runner import _run_snapgrid_local
    from meshpipeline.engines.snappy.snappy_runner import _run_snappy_local
    from meshpipeline.engines.snappy_multiregion.multiregion_runner import _run_snappy_multiregion_local
    from meshpipeline.engines.vmtk.vmtk_runner import _run_vmtk_local
    return {
        "cfmesh":             _run_cartesian_mesh_local,
        "cfmesh_conjugate":   _run_conjugate_local,
        "snappy":             _run_snappy_local,
        "snappy_multiregion": _run_snappy_multiregion_local,
        "snapgrid":           _run_snapgrid_local,
        "gmsh":               _run_gmsh_local,
        "vmtk":               _run_vmtk_local,
    }


def _run_stamped(runner, workspace: Any, *, engine: str, bashrc: str, timeout: int) -> Any:
    """Run `runner` pinned to the determinism settings (engines/determinism.py) and stamp the
    result: the case's hashes before the run, the threads/ranks/seeds/versions it ran with, and
    every time a clock changed a decision. The stamp rides home beside the mesh (STAMP_FILE) and
    in the result (`reproducibility`). A stamp that cannot be made never fails a run."""
    import logging
    import time

    from meshpipeline.engines import determinism as det

    log = logging.getLogger(__name__)
    started = time.monotonic()
    try:
        hashes = det.workspace_hashes(workspace)
    except Exception:  # noqa: BLE001 - the stamp is a record, never a prerequisite
        log.warning("reproducibility: could not hash the case", exc_info=True)
        hashes = {}
    det.pin_process_environment(engine)
    with det.recording() as rec:
        out = runner(workspace, bashrc=bashrc, timeout=int(timeout))
        if isinstance(out, dict) and out.get("timed_out"):
            det.note_time_limit(engine, "the native run was stopped at its time limit",
                                budget_s=float(timeout))
        try:
            foam = ""
            if engine in _OPENFOAM_ENGINES or engine == "snapgrid":
                from meshpipeline.engines.snappy.native import _foam_version
                foam = _foam_version(bashrc)
            stamp = det.stamp(engine, record=rec, hashes=hashes, foam_version=foam,
                              started=started)
            det.write_stamp(workspace, stamp)
            if isinstance(out, dict):
                out["reproducibility"] = stamp
        except Exception:  # noqa: BLE001
            log.warning("reproducibility: could not stamp the run", exc_info=True)
    return out


def run_engine_local(workspace: Any, *, engine: str, timeout: int) -> dict:
    from importlib import import_module

    runners = engine_runners()
    if engine not in runners:
        return {"rc": -4, "timed_out": False,
                "log_tail": f"[DISPATCH] unknown engine {engine!r} - known: {sorted(runners)}. "
                            "The mesh image is likely stale; redeploy it."}
    reason = (import_module(f"meshpipeline.engines.{engine}.foam_exec").scan_case_dicts(workspace)
              if engine in _OPENFOAM_ENGINES else None)
    if reason:
        return {"rc": -2, "timed_out": False, "log_tail": f"REJECTED: {reason}"}
    from meshpipeline.engines.cfmesh.foam_exec import _DEFAULT_BASHRC  # identical across the clones
    out = _run_stamped(runners[engine], workspace, engine=engine, bashrc=_DEFAULT_BASHRC,
                       timeout=int(timeout))
    # WHAT RIDES HOME (engines/return_scope.py): the workspace is archived and extracted under a
    # size ceiling after this; scratch and, when it would not fit, the optional volume export stay
    # behind - a built mesh is never lost on the way back for a file the delivery can do without
    #
    # AND IT IS WATCHED, because this is the longest silent window in the whole run. `return_scope`
    # says so in its own header - "Every byte of the workspace is compressed here to decide, and
    # then again by the archiver to send" - and nothing in it writes a workspace log, so from the
    # job's point of view the run stopped making progress the moment the engine returned. The
    # beacon's own `returning` flag is set by `runtime/mesh_runner.run` only AFTER this function
    # returns, so it covers the upload and not the measure: at the plate's 590 GiB on disk the two
    # measure passes alone are 14,921 s of silence against a PROGRESS_GRACE_S of 1,020. See
    # WATCH_EVERY_S.
    with watching_progress(workspace, phase=RETURN_PHASE):
        try:
            from meshpipeline.engines.return_scope import scope_return
            # THE RESULT TRAVELS WITH IT so the MEASURED cell count raises the ceiling: the mesher
            # knows exactly how many cells it just wrote, and the extractor waiting for the archive
            # will use that number too (result_extraction_limits).
            scope = scope_return(workspace, result=out if isinstance(out, dict) else None)
        except Exception:  # noqa: BLE001 - trimming is an economy, never a reason to fail a run
            import logging
            logging.getLogger(__name__).warning("return scope failed; the workspace goes as it is",
                                                exc_info=True)
            scope = None
        if scope and isinstance(out, dict):
            out["return_scope"] = scope
        if isinstance(out, dict):
            # WHAT IS SENT HOME, MEASURED: the worker sizes the bounded extraction of an FEA mesh by
            # these bytes (adapters/mesh_execution/cloud_run_client.result_extraction_limits), so a
            # valid FEA mesh is never lost on the way back for being bigger than a flow mesh's
            # per-cell guess. A stat walk over every file of the plate's workspace, inside the watch
            # for the same reason the measure above is.
            try:
                out["returned_bytes"] = returned_bytes(workspace)
            except Exception:  # noqa: BLE001 - a measurement, never a reason to fail a run
                pass
    return out


def returned_bytes(workspace: Any) -> dict:
    """{raw, largest, fea}: the workspace's total file bytes, its largest file and whether it is an
    FEA plan's workspace - measured on the mesher just before the workspace is archived."""
    from pathlib import Path
    ws = Path(workspace)
    raw = largest = 0
    for f in ws.rglob("*"):
        if f.is_file() and not f.is_symlink():
            n = f.stat().st_size
            raw += n
            largest = max(largest, n)
    return {"raw": int(raw), "largest": int(largest), "fea": (ws / "fea_plan.json").exists()}
