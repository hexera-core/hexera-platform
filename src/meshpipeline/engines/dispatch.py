# Responsibility: Map an engine name to the callable that runs it in-process.
# Boundaries: dispatch only; every runner it names owns its own native lifecycle.
# Collaborates with: the six engine runners and adapters/mesh_execution/local.py.
from __future__ import annotations

from typing import Any

# OpenFOAM engines carry LLM-authored dicts that must be scanned before executing; the
# scan is engine-owned (each bundle carries its own foam_exec clone). gmsh/vmtk author no
# such dicts.
_OPENFOAM_ENGINES = frozenset({"cfmesh", "snappy", "snappy_multiregion"})


def engine_runners() -> dict:
    from meshpipeline.engines.cfmesh.native import _run_cartesian_mesh_local
    from meshpipeline.engines.gmsh.gmsh_runner import _run_gmsh_local
    from meshpipeline.engines.snapgrid.runner import _run_snapgrid_local
    from meshpipeline.engines.snappy.snappy_runner import _run_snappy_local
    from meshpipeline.engines.snappy_multiregion.multiregion_runner import _run_snappy_multiregion_local
    from meshpipeline.engines.vmtk.vmtk_runner import _run_vmtk_local
    return {
        "cfmesh":             _run_cartesian_mesh_local,
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
    try:
        from meshpipeline.engines.return_scope import scope_return
        scope = scope_return(workspace)
    except Exception:  # noqa: BLE001 - trimming is an economy, never a reason to fail a run
        import logging
        logging.getLogger(__name__).warning("return scope failed; the workspace goes as it is",
                                            exc_info=True)
        scope = None
    if scope and isinstance(out, dict):
        out["return_scope"] = scope
    if isinstance(out, dict):
        # WHAT IS SENT HOME, MEASURED: the worker sizes the bounded extraction of an FEA mesh by these
        # bytes (adapters/mesh_execution/cloud_run_client.result_extraction_limits), so a valid FEA
        # mesh is never lost on the way back for being bigger than a flow mesh's per-cell guess
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
