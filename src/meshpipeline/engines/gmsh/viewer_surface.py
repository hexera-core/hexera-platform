# Responsibility: Expose the surface the browser viewer draws for a Gmsh result, and the volume
# behind it so the viewer can colour that surface by cell quality.
# Boundaries: presentation of a delivered mesh; it judges nothing.
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def read_msh(workspace) -> dict | None:
    """mesh.msh as the viewer needs it: points (P,3), the volume cells by kind (corner nodes
    only - a second-order element lists its corners first, and gmsh says how many), and the
    named boundary groups as polygons of point indices, in the order the surface is drawn.
    None when there is no mesh yet; {} when the file could not be read (logged)."""
    msh = Path(workspace) / "mesh.msh"
    if not msh.exists() or msh.stat().st_size == 0:
        return None
    import gmsh
    import numpy as np
    _mine = not gmsh.isInitialized()
    if _mine:
        gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.open(str(msh))
        node_tags, coords, _ = gmsh.model.mesh.getNodes()
        node_tags = np.asarray(node_tags, dtype=np.int64)
        points = np.asarray(coords, dtype=np.float64).reshape(-1, 3)
        index_of = np.full(int(node_tags.max()) + 1 if len(node_tags) else 1, -1, dtype=np.int64)
        index_of[node_tags] = np.arange(len(node_tags))

        def corners(et, nodes):
            _n, _d, _o, n_nodes, _c, n_primary = gmsh.model.mesh.getElementProperties(int(et))
            rows = np.asarray(nodes, dtype=np.int64).reshape(-1, int(n_nodes))[:, :int(n_primary)]
            return index_of[rows]

        cells: list = []
        etypes, _tags, enodes = gmsh.model.mesh.getElements(3)
        for et, nodes in zip(etypes, enodes):
            block = corners(et, nodes)
            if block.shape[1] in (4, 5, 6, 8) and len(block):
                cells.append(block)

        patches: list = []
        for dim, ptag in gmsh.model.getPhysicalGroups(2):
            name = gmsh.model.getPhysicalName(dim, ptag) or f"group_{ptag}"
            polys: list = []
            for ent in gmsh.model.getEntitiesForPhysicalGroup(dim, ptag):
                etypes, _etags, enodes = gmsh.model.mesh.getElements(dim, ent)
                for et, nodes in zip(etypes, enodes):
                    block = corners(et, nodes)
                    if block.shape[1] in (3, 4):
                        polys.extend(block.tolist())
            if polys:
                patches.append((name, polys))
        gmsh.model.remove()
        return {"points": points, "cells": cells, "patches": patches}
    except Exception as exc:  # noqa: BLE001 - the viewer degrades, it never fails a job
        logger.warning("gmsh viewer surface parse failed: %s", exc)
        return {}
    finally:
        if _mine:
            gmsh.finalize()


def surface_patches_of(mesh: dict | None) -> dict[str, list]:
    """The drawn surface: patch name -> triangles as coordinate triples. A quad is drawn as two
    fan triangles, in the order render/volume_quality counts them."""
    if not mesh:
        return {}
    points = mesh["points"]
    out: dict[str, list] = {}
    for name, polys in mesh["patches"]:
        tris: list = []
        for poly in polys:
            p = [tuple(float(c) for c in points[i]) for i in poly]
            for k in range(1, len(p) - 1):
                tris.append([p[0], p[k], p[k + 1]])
        if tris:
            out[name] = tris
    return out


def surface_patches(workspace) -> dict[str, list]:
    return surface_patches_of(read_msh(workspace))
