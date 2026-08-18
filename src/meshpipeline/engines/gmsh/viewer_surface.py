# Responsibility: Expose the surface the browser viewer draws for a Gmsh result.
# Boundaries: presentation of a delivered mesh; it judges nothing.
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# nodes-per-element for the gmsh surface types we emit (corner count is 3)
_TRI_TYPES = {2: 3, 9: 6}   # 3-node tri, 6-node (second-order) tri


def surface_patches(workspace) -> dict[str, list]:
    msh = Path(workspace) / "mesh.msh"
    if not msh.exists() or msh.stat().st_size == 0:
        return {}
    import gmsh
    _mine = not gmsh.isInitialized()
    if _mine:
        gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.open(str(msh))
        node_tags, coords, _ = gmsh.model.mesh.getNodes()
        xyz = {int(t): (coords[3 * i], coords[3 * i + 1], coords[3 * i + 2])
               for i, t in enumerate(node_tags)}
        out: dict[str, list] = {}
        for dim, ptag in gmsh.model.getPhysicalGroups(2):
            name = gmsh.model.getPhysicalName(dim, ptag) or f"group_{ptag}"
            tris: list = []
            for ent in gmsh.model.getEntitiesForPhysicalGroup(dim, ptag):
                etypes, _etags, enodes = gmsh.model.mesh.getElements(dim, ent)
                for et, nodes in zip(etypes, enodes):
                    npr = _TRI_TYPES.get(int(et))
                    if not npr:
                        continue
                    for i in range(0, len(nodes), npr):
                        tris.append([xyz[int(nodes[i + k])] for k in range(3)])
            if tris:
                out[name] = tris
        gmsh.model.remove()
        return out
    except Exception as exc:
        logger.warning("gmsh viewer surface parse failed: %s", exc)
        return {}
    finally:
        if _mine:
            gmsh.finalize()
