# Responsibility: Turn snappyHexMesh's polyMesh boundary into a viewer payload, or nothing when no mesh was written.
# Boundaries: it locates the case directory; the surface reader and the viewer packer do the work.

# ENGINE-LOCAL copy of the retired engines/openfoam_common/viewer.py (deliberate
# duplication: each OpenFOAM-family bundle owns its stack; divergence allowed).
from __future__ import annotations

from pathlib import Path


def polymesh_viewer(workspace, *, roles: dict, units: str,
                    skip_names: tuple[str, ...] = ()) -> dict | None:
    pm = Path(workspace) / "constant" / "polyMesh"
    if not (pm / "boundary").exists():
        return None
    from meshpipeline.engines.snappy.polymesh_surface import boundary_surface
    from meshpipeline.render.viewer_pack import polymesh_response
    raw, cell_stats = boundary_surface(pm, skip_names=skip_names)
    return polymesh_response(raw, cell_stats, roles, units)
