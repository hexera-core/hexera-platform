# Responsibility: Report the mesher library versions a verdict was produced with.
# Owns: the `mesher_versions` kernel call.
# Boundaries: runs inside a kernel worker only; the parent never imports a mesher.
"""A mesher upgrade can change what meshes, so the versions are part of the verdict cache key."""

from __future__ import annotations

from meshpipeline.cad.repair_v4.kernel_calls import register


def _version(module: str, attr: str = "__version__") -> str:
    import importlib

    try:
        return str(getattr(importlib.import_module(module), attr))
    except Exception:  # noqa: BLE001 - a missing mesher is a fact to key on, not an error
        return "absent"


@register("mesher_versions")
def _mesher_versions(payload: dict) -> dict:
    return {"gmsh": _version("gmsh"), "netgen": _version("netgen"), "ocp": _version("OCP")}
