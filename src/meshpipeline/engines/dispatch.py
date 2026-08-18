# Responsibility: Map an engine name to the callable that runs it in-process.
# Boundaries: dispatch only; every runner it names owns its own native lifecycle.
# Collaborates with: the five engine runners and adapters/mesh_execution/local.py.
from __future__ import annotations

from typing import Any

# OpenFOAM engines carry LLM-authored dicts that must be scanned before executing; the
# scan is engine-owned (each bundle carries its own foam_exec clone). gmsh/vmtk author no
# such dicts.
_OPENFOAM_ENGINES = frozenset({"cfmesh", "snappy", "snappy_multiregion"})


def engine_runners() -> dict:
    from meshpipeline.engines.cfmesh.native import _run_cartesian_mesh_local
    from meshpipeline.engines.gmsh.gmsh_runner import _run_gmsh_local
    from meshpipeline.engines.snappy.snappy_runner import _run_snappy_local
    from meshpipeline.engines.snappy_multiregion.multiregion_runner import _run_snappy_multiregion_local
    from meshpipeline.engines.vmtk.vmtk_runner import _run_vmtk_local
    return {
        "cfmesh":             _run_cartesian_mesh_local,
        "snappy":             _run_snappy_local,
        "snappy_multiregion": _run_snappy_multiregion_local,
        "gmsh":               _run_gmsh_local,
        "vmtk":               _run_vmtk_local,
    }


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
    return runners[engine](workspace, bashrc=_DEFAULT_BASHRC, timeout=int(timeout))
