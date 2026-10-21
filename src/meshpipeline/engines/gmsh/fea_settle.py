# Responsibility: Settle a curved FEA mesh after gmsh has placed its midside nodes on the CAD: an element curving left worse than straight keeps straight sides (PLAN-fea-11), and the elements touching a CAD cusp get straight sides and are set aside from the quality floor (PLAN-fea-10).
# Boundaries: the current gmsh model only, in-process inside the FEA adapter; it moves midside nodes and nothing else (no corner node moves, no element is added or removed). It decides which elements are set aside; the adapter reports them and the verdict names them.
# Collaborates with: engines/gmsh/fea_adapter.py (calls it between setOrder and the deck), engines/fea_plan.py (the cusps, in metres), cad/fea_measure.cad_cusps (where they come from), docs/RULES.md PLAN-fea-10 / PLAN-fea-11.
"""SETTLING A CURVED MESH.

gmsh curves a quadratic mesh by putting each boundary edge's midside node on the CAD surface. Two
things can go wrong with a few elements, and neither is fixed by meshing again:

* CURVING MAKES IT WORSE (PLAN-fea-11). A thin element beside a tight round can fold when its
  midside nodes move onto the surface (a sprocket's teeth: SICN 0.01 straight, -0.14 curved). Such
  an element keeps straight sides: its midside nodes go back to the middle of its edges. Its
  shape is then its straight shape, always valid; the surface it sits on is off by the chord's
  sagitta there, a few microns.
* A CUSP (PLAN-fea-10). Two CAD edges of one face leave a vertex in the same direction (a bore
  just touching a straight face): the material runs to nothing there, and no element there can be
  well shaped at any size (Areen's bearing bracket, job ab20566f: every optimiser, the other 3D
  algorithm, local refinement and other 2D algorithms all left the same slivers). The elements
  touching a cusp get straight sides and are SET ASIDE from the quality floor; every other element
  still has to meet it, and the delivery names each cusp (Areen's decision, 21 Oct).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

import numpy as np

#: An element touches a cusp when one of its corners lies within this many of its own longest edges
#: of the cusp point.
CUSP_REACH = 2.0
#: gmsh's 10-node tetrahedron: midside node 4+k sits on the edge between these corners (gmsh's own
#: numbering; engines/gmsh/abaqus_deck.GMSH_TET10_TO_SOLVER reads the same). Matching a midside node
#: to the nearest edge middle instead goes wrong on exactly the elements curving bent the most.
GMSH_TET10_EDGES = ((0, 1), (1, 2), (2, 0), (0, 3), (2, 3), (1, 3))
_EDGES = [(i, j) for i in range(4) for j in range(i + 1, 4)]


def qualities(gmsh) -> tuple[np.ndarray, np.ndarray]:
    """(element tags, minSICN) of every volume element of the current model."""
    _types, tags, _nodes = gmsh.model.mesh.getElements(3)
    t = np.concatenate([np.asarray(x, dtype=np.int64) for x in tags]) if len(tags) else np.zeros(0, dtype=np.int64)
    q = np.asarray(gmsh.model.mesh.getElementQualities(t.tolist(), "minSICN"), dtype=float) if len(t) else np.zeros(0)
    return t, q


def _corners(gmsh, tag: int) -> tuple[list[int], np.ndarray]:
    _typ, nodes, _dim, _ent = gmsh.model.mesh.getElement(int(tag))
    return [int(n) for n in nodes], np.array([gmsh.model.mesh.getNode(int(n))[0] for n in nodes[:4]], dtype=float)


def straighten(gmsh, tag: int) -> int:
    """Put the element's midside nodes back on its straight edges; how many moved. A midside node is
    shared with the neighbours on that edge, which become straight on that edge too."""
    nodes, c = _corners(gmsh, tag)
    moved = 0
    for n, (i, j) in zip(nodes[4:10], GMSH_TET10_EDGES, strict=False):
        mid = 0.5 * (c[i] + c[j])
        if float(np.linalg.norm(np.array(gmsh.model.mesh.getNode(n)[0], dtype=float) - mid)) > 0.0:
            gmsh.model.mesh.setNode(n, mid.tolist(), [])
            moved += 1
    return moved


def settle(gmsh, straight: Mapping[int, float], cusps_m: Iterable, floor: float) -> dict:
    """PLAN-fea-11 then PLAN-fea-10 on the current (curved) model. `straight` is each element's
    minSICN before curving (tag -> value). Returns {"straightened": n, "set_aside": [tags],
    "cusp_places_m": [[x, y, z], ...] (the cusps that had elements set aside)}."""
    tags, q = qualities(gmsh)
    straightened = 0
    for t, qq in zip(tags.tolist(), q.tolist(), strict=True):
        if qq < floor and qq < float(straight.get(t, 1.0)):
            straighten(gmsh, t)
            straightened += 1
    pts = np.asarray([list(c) for c in cusps_m], dtype=float).reshape(-1, 3)
    set_aside: list[int] = []
    places: set[int] = set()
    if len(pts):
        tags, q = qualities(gmsh)
        for t in tags[q < floor].tolist():
            _nodes, c = _corners(gmsh, t)
            h = max(float(np.linalg.norm(c[i] - c[j])) for i, j in _EDGES)
            d = np.linalg.norm(pts[:, None, :] - c[None, :, :], axis=2).min(axis=1)
            k = int(np.argmin(d))
            if d[k] < CUSP_REACH * h:
                straighten(gmsh, t)
                set_aside.append(int(t))
                places.add(k)
    return {"straightened": straightened, "set_aside": set_aside,
            "cusp_places_m": [pts[k].round(9).tolist() for k in sorted(places)]}


__all__ = ["CUSP_REACH", "GMSH_TET10_EDGES", "qualities", "settle", "straighten"]
