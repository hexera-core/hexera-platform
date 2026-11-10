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
* A MICRO DETAIL (PLAN-fea-12). An edge or a face far smaller than the elements there (a sprocket's
  teeth drawn with 0.001 mm flat strips, a socket's 0.004 mm edges) pins the elements on it into bad
  shapes the same way. They are set aside the same way, and the delivery names each detail - and
  says that stress right at it, a gear's tooth-tip contact stress included, is not resolved.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

import numpy as np

#: An element touches a cusp when one of its corners lies within this many of its own longest edges
#: of the cusp point.
CUSP_REACH = 2.0
#: ...and a micro detail (PLAN-fea-12) when one of its corners lies this close to a point along it.
MICRO_REACH = 2.0
#: PLAN-fea-11: at most this many more rounds of straightening the elements a straightened neighbour turned
#: inside out (each round straightens at least one curved edge; ox_mc_lid's plates needed 2).
SETTLE_ROUNDS = 50
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


def _straighten_nodes(gmsh, tag: int) -> list[int]:
    nodes, c = _corners(gmsh, tag)
    moved = []
    for n, (i, j) in zip(nodes[4:10], GMSH_TET10_EDGES, strict=False):
        mid = 0.5 * (c[i] + c[j])
        if float(np.linalg.norm(np.array(gmsh.model.mesh.getNode(n)[0], dtype=float) - mid)) > 0.0:
            gmsh.model.mesh.setNode(n, mid.tolist(), [])
            moved.append(int(n))
    return moved


def straighten(gmsh, tag: int) -> int:
    """Put the element's midside nodes back on its straight edges; how many moved. A midside node is
    shared with the neighbours on that edge, which become straight on that edge too."""
    return len(_straighten_nodes(gmsh, tag))


#: PLAN-fea-11's rounds: straightening an element straightens its neighbours on the shared edges too,
#: and a neighbour left half straight, half curved can fold (dev-areen 9 Nov: 590 straightened in one
#: round left 4 of 6.47M inverted - each straight on 4 edges by its neighbours, curved on 2 - and the
#: whole mesh was refused). Each round looks again only at the elements that share a node moved in the
#: last one. A round only ever makes edges straight, so the rounds end; this is the backstop.
MAX_ROUNDS = 50


def _volume_nodes(gmsh) -> tuple[np.ndarray, np.ndarray]:
    """(element tags, midside node ids per element) of every 10-node tetrahedron of the current model."""
    types, tags, nodes = gmsh.model.mesh.getElements(3)
    ts, ms = [], []
    for typ, t, n in zip(types, tags, nodes, strict=True):
        k = int(gmsh.model.mesh.getElementProperties(int(typ))[3])
        if k == 10:
            ts.append(np.asarray(t, dtype=np.int64))
            ms.append(np.asarray(n, dtype=np.int64).reshape(-1, 10)[:, 4:])
    if not ts:
        return np.zeros(0, dtype=np.int64), np.zeros((0, 6), dtype=np.int64)
    return np.concatenate(ts), np.concatenate(ms)


def _straighten_until_settled(gmsh, straight: Mapping[int, float], floor: float) -> tuple[int, int]:
    """PLAN-fea-11 to the end: (elements straightened, rounds)."""
    tags, q = qualities(gmsh)
    done: set[int] = set()
    todo = [t for t, qq in zip(tags.tolist(), q.tolist(), strict=True)
            if qq < floor and qq < float(straight.get(t, 1.0))]
    mids: tuple[np.ndarray, np.ndarray] | None = None
    rounds = 0
    while todo and rounds < MAX_ROUNDS:
        rounds += 1
        moved: list[int] = []
        for t in todo:
            done.add(int(t))
            moved += _straighten_nodes(gmsh, t)
        if not moved:
            break
        if mids is None:
            mids = _volume_nodes(gmsh)
        near = mids[0][np.isin(mids[1], np.asarray(moved, dtype=np.int64)).any(axis=1)]
        near = np.asarray([t for t in near.tolist() if t not in done], dtype=np.int64)
        if not len(near):
            break
        qn = gmsh.model.mesh.getElementQualities(near.tolist(), "minSICN")
        todo = [t for t, qq in zip(near.tolist(), qn, strict=True)
                if qq < floor and qq < float(straight.get(t, 1.0))]
    return len(done), rounds


def settle(gmsh, straight: Mapping[int, float], cusps_m: Iterable, floor: float,
           micro: Iterable[Mapping] = ()) -> dict:
    """PLAN-fea-11, then PLAN-fea-10 and PLAN-fea-12 on the current (curved) model. `straight` is
    each element's minSICN before curving (tag -> value); `micro` the plan's micro details, each with
    `points_m` along it. Returns {"straightened": n, "rounds": n, "set_aside": [tags], "cusp_places_m":
    [...] (the cusps that had elements set aside), "cusp_elements": n, "micro_used": [index into micro],
    "micro_elements": n}."""
    straightened, rounds = _straighten_until_settled(gmsh, straight, floor)
    pts = np.asarray([list(c) for c in cusps_m], dtype=float).reshape(-1, 3)
    details = list(micro or ())
    mpts, mown = [], []
    for i, m in enumerate(details):
        for p in m.get("points_m") or []:
            mpts.append(p)
            mown.append(i)
    mtree = None
    if mpts:
        from scipy.spatial import cKDTree

        mtree = cKDTree(np.asarray(mpts, dtype=float))
    set_aside: list[int] = []
    places: set[int] = set()
    used: set[int] = set()
    n_cusp = n_micro = 0
    if len(pts) or mtree is not None:
        tags, q = qualities(gmsh)
        for t in tags[q < floor].tolist():
            _nodes, c = _corners(gmsh, t)
            h = max(float(np.linalg.norm(c[i] - c[j])) for i, j in _EDGES)
            if len(pts):
                d = np.linalg.norm(pts[:, None, :] - c[None, :, :], axis=2).min(axis=1)
                k = int(np.argmin(d))
                if d[k] < CUSP_REACH * h:
                    straighten(gmsh, t)
                    set_aside.append(int(t))
                    places.add(k)
                    n_cusp += 1
                    continue
            if mtree is not None:
                dist, idx = mtree.query(c)
                j = int(np.argmin(dist))
                if float(dist[j]) < MICRO_REACH * h:
                    straighten(gmsh, t)
                    set_aside.append(int(t))
                    used.add(mown[int(idx[j])])
                    n_micro += 1
    return {"straightened": straightened, "rounds": rounds, "set_aside": set_aside,
            "cusp_places_m": [pts[k].round(9).tolist() for k in sorted(places)], "cusp_elements": n_cusp,
            "micro_used": sorted(used), "micro_elements": n_micro}


__all__ = ["CUSP_REACH", "GMSH_TET10_EDGES", "MAX_ROUNDS", "MICRO_REACH", "qualities", "settle", "straighten"]
