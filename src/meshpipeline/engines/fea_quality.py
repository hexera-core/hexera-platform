# Responsibility: Measure an FEA solid mesh's element quality the way the target solvers judge it - the Jacobian over the whole curved element, the scaled Jacobian, the Jacobian ratio, the aspect ratio and the dihedral angles - and hold each to the strictest limit among the version-1 solvers.
# Boundaries: engine-neutral and pure numpy over the internal mesh (engines/gmsh/abaqus_deck.SolidMesh: nodes, 4- or 10-node tetrahedra in solver order). It decides no verdict: the gate (VER-1) and the production limits (VER-2) read these numbers (engines/gmsh/gates.py, engines/verdict_limits.py).
# Collaborates with: engines/gmsh/fea_adapter.py (measures every FEA mesh into quality.json), engines/fea_results.py (says the numbers in words), docs/RULES.md (VER-2 rows for FEA meshes).
"""FEA ELEMENT QUALITY, MEASURED.

Per element (corner nodes 1-4, then the edge midpoints 1-2, 2-3, 3-1, 1-4, 2-4, 3-4: the Abaqus
and CalculiX order the deck writes):

* JACOBIAN: the determinant of the map from the reference tetrahedron, sampled at the 10 nodes and
  the centroid - the whole curved element, not only its corners (Johnen et al., CAD 103, 2018).
  Not above zero anywhere means the element is inverted: no solver assembles it (VER-1).
* SCALED JACOBIAN: Verdict's scaled Jacobian of the straight-sided element (1 for a perfect
  tetrahedron), times the curved element's smallest Jacobian over its straight one.
* JACOBIAN RATIO: the largest Jacobian over the smallest across the nodes (Ansys's corner-node
  form; 1 for a straight element).
* ASPECT RATIO: the longest edge over the shortest height (corner to opposite face), divided by
  that ratio for a perfect tetrahedron, so a perfect one reads 1.
* DIHEDRAL ANGLES: the angles between each pair of faces (70.5 degrees for a perfect one).

THE LIMITS (Hexera's internal gate: the strictest of the version-1 solvers, design section 9;
sources in Documents/HEXERA_FEA_RESEARCH.md section 3):
  scaled Jacobian at least 0.2 (Cubit/Verdict's acceptable range for tetrahedra);
  Jacobian ratio at most 30 (Ansys MAPDL's corner-node warning);
  aspect ratio at most 20 (Ansys MAPDL's warning; Abaqus warns on its own measure at 10);
  dihedral angles between 5 and 170 degrees.
"""
from __future__ import annotations

import math

import numpy as np

#: (key, label, op, limit, source) - the FEA rows of the production limits (VER-2)
LIMITS: tuple[tuple[str, str, str, float, str], ...] = (
    ("fea_min_scaled_jacobian", "Worst scaled Jacobian (1 is a perfect element)", ">=", 0.2,
     "Cubit/Verdict acceptable range for tetrahedra (0.2 to 1)"),
    ("fea_max_jacobian_ratio", "Worst Jacobian ratio across an element's nodes", "<=", 30.0,
     "Ansys MAPDL shape check, corner-node Jacobian ratio warning"),
    ("fea_max_aspect_ratio", "Worst aspect ratio (1 is a perfect element)", "<=", 20.0,
     "Ansys MAPDL shape check, aspect ratio warning"),
    ("fea_min_dihedral_deg", "Smallest angle between two faces of an element (degrees)", ">=", 5.0,
     "Hexera's internal gate: the dihedral range 5 to 170 degrees"),
    ("fea_max_dihedral_deg", "Largest angle between two faces of an element (degrees)", "<=", 170.0,
     "Hexera's internal gate: the dihedral range 5 to 170 degrees"),
)

#: the reference tetrahedron's points the Jacobian is sampled at: the 4 corners, the 6 edge
#: midpoints (in the deck's order) and the centroid, as (xi, eta, zeta)
_CORNERS = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=float)
_EDGES = ((0, 1), (1, 2), (2, 0), (0, 3), (1, 3), (2, 3))
_SAMPLES = np.vstack([_CORNERS, [(_CORNERS[a] + _CORNERS[b]) / 2 for a, b in _EDGES], [[0.25, 0.25, 0.25]]])
#: a perfect tetrahedron's longest edge over its shortest height: 1 / sqrt(2/3)
_PERFECT_ASPECT = 1.0 / math.sqrt(2.0 / 3.0)
_CHUNK = 50_000


def _dN10(p: np.ndarray) -> np.ndarray:
    """The 10-node tetrahedron's shape-function gradients (10, 3) at reference point p."""
    xi, eta, zeta = p
    L = np.array([1 - xi - eta - zeta, xi, eta, zeta])
    dL = np.array([[-1, -1, -1], [1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=float)
    d = np.zeros((10, 3))
    for i in range(4):
        d[i] = (4 * L[i] - 1) * dL[i]
    for k, (a, b) in enumerate(_EDGES):
        d[4 + k] = 4 * (L[a] * dL[b] + L[b] * dL[a])
    return d


_DN = np.stack([_dN10(p) for p in _SAMPLES])          # (11, 10, 3)


def _det3(m: np.ndarray) -> np.ndarray:
    return (m[..., 0, 0] * (m[..., 1, 1] * m[..., 2, 2] - m[..., 1, 2] * m[..., 2, 1])
            - m[..., 0, 1] * (m[..., 1, 0] * m[..., 2, 2] - m[..., 1, 2] * m[..., 2, 0])
            + m[..., 0, 2] * (m[..., 1, 0] * m[..., 2, 1] - m[..., 1, 1] * m[..., 2, 0]))


def element_measures(X: np.ndarray) -> dict[str, np.ndarray]:
    """Per element, for coordinates X (E, 4|10, 3): min_detj, jac_ratio, scaled_jacobian,
    aspect, min_dihedral, max_dihedral (degrees)."""
    P = X[:, :4, :]
    e01, e02, e03 = P[:, 1] - P[:, 0], P[:, 2] - P[:, 0], P[:, 3] - P[:, 0]
    det_lin = np.einsum("ij,ij->i", e03, np.cross(e01, e02))    # 6 x volume
    if X.shape[1] == 10:
        J = np.einsum("eak,saj->sekj", X, _DN)                 # (11, E, 3, 3)
        dets = _det3(J)                                        # (11, E)
        min_detj = dets.min(axis=0)
        node = dets[:10]
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = np.where(node.min(axis=0) > 0, node.max(axis=0) / node.min(axis=0), np.inf)
    else:
        min_detj = det_lin
        ratio = np.where(det_lin > 0, 1.0, np.inf)
    # Verdict's scaled Jacobian of the straight element
    L0, L1, L2 = P[:, 1] - P[:, 0], P[:, 2] - P[:, 1], P[:, 0] - P[:, 2]
    L3, L4, L5 = P[:, 3] - P[:, 0], P[:, 3] - P[:, 1], P[:, 3] - P[:, 2]
    n = [np.linalg.norm(v, axis=1) for v in (L0, L1, L2, L3, L4, L5)]
    jacobi = np.einsum("ij,ij->i", L3, np.cross(L2, L0))
    lmax = np.max(np.stack([n[0] * n[2] * n[3], n[0] * n[1] * n[4], n[1] * n[2] * n[5], n[3] * n[4] * n[5]]), axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        sj_lin = np.where(lmax > 0, jacobi * math.sqrt(2.0) / lmax, 0.0)
        curved = np.where(det_lin > 0, np.minimum(min_detj / det_lin, 1.0), 0.0)
    sj = np.where(min_detj > 0, sj_lin * curved, np.minimum(sj_lin, 0.0) - (min_detj <= 0))
    # aspect: longest edge over shortest height (6V = |det|; height to face = 3V / face area)
    longest = np.max(np.stack(n), axis=0)
    faces = ((1, 2, 3), (0, 2, 3), (0, 1, 3), (0, 1, 2))
    areas = np.stack([0.5 * np.linalg.norm(np.cross(P[:, b] - P[:, a], P[:, c] - P[:, a]), axis=1) for a, b, c in faces])
    vol = np.abs(det_lin) / 6.0
    with np.errstate(divide="ignore", invalid="ignore"):
        hmin = np.min(3.0 * vol / np.maximum(areas, 1e-300), axis=0)
        aspect = np.where(hmin > 0, longest / hmin / _PERFECT_ASPECT, np.inf)
    # dihedral angles: between the outward normals of each pair of faces, pi minus their angle
    normals = []
    for k, (a, b, c) in enumerate(faces):
        nrm = np.cross(P[:, b] - P[:, a], P[:, c] - P[:, a])
        opp = P[:, k] - P[:, a]
        s = np.where(np.einsum("ij,ij->i", nrm, opp) > 0, -1.0, 1.0)      # point away from the 4th corner
        nrm = nrm * s[:, None]
        normals.append(nrm / np.maximum(np.linalg.norm(nrm, axis=1), 1e-300)[:, None])
    angs = []
    for i in range(4):
        for j in range(i + 1, 4):
            c = np.clip(np.einsum("ij,ij->i", normals[i], normals[j]), -1.0, 1.0)
            angs.append(180.0 - np.degrees(np.arccos(c)))
    A = np.stack(angs)
    return {"min_detj": min_detj, "jac_ratio": ratio, "scaled_jacobian": sj, "aspect": aspect,
            "min_dihedral": A.min(axis=0), "max_dihedral": A.max(axis=0)}


def measure(mesh) -> dict:
    """The mesh's worst figures and how many elements miss each limit. `mesh`: SolidMesh."""
    ids = np.asarray(mesh.node_ids)
    index = np.full(int(ids.max()) + 1, -1, dtype=np.int64)
    index[ids] = np.arange(len(ids))
    xyz = np.asarray(mesh.xyz, dtype=float)
    els = index[np.asarray(mesh.elements)]
    worst = {"min_detj": math.inf, "scaled_jacobian": math.inf, "jac_ratio": 0.0, "aspect": 0.0,
             "min_dihedral": 180.0, "max_dihedral": 0.0}
    inverted = 0
    over = {k: 0 for k, *_ in LIMITS}
    where = {}
    for s in range(0, len(els), _CHUNK):
        X = xyz[els[s:s + _CHUNK]]
        m = element_measures(X)
        inverted += int(np.count_nonzero(m["min_detj"] <= 0))
        cen = X[:, :4].mean(axis=1)
        for key, arr, low in (("scaled_jacobian", m["scaled_jacobian"], True), ("jac_ratio", m["jac_ratio"], False),
                              ("aspect", m["aspect"], False), ("min_dihedral", m["min_dihedral"], True),
                              ("max_dihedral", m["max_dihedral"], False), ("min_detj", m["min_detj"], True)):
            i = int(np.argmin(arr) if low else np.argmax(arr))
            v = float(arr[i])
            if (low and v < worst[key]) or (not low and v > worst[key]):
                worst[key] = v
                where[key] = [round(float(c), 6) for c in cen[i]]
        over["fea_min_scaled_jacobian"] += int(np.count_nonzero(m["scaled_jacobian"] < 0.2))
        over["fea_max_jacobian_ratio"] += int(np.count_nonzero(m["jac_ratio"] > 30.0))
        over["fea_max_aspect_ratio"] += int(np.count_nonzero(m["aspect"] > 20.0))
        over["fea_min_dihedral_deg"] += int(np.count_nonzero(m["min_dihedral"] < 5.0))
        over["fea_max_dihedral_deg"] += int(np.count_nonzero(m["max_dihedral"] > 170.0))

    def r(v: float) -> float | None:
        return None if not math.isfinite(v) else float(f"{v:.4g}")

    figures = {"fea_min_scaled_jacobian": r(worst["scaled_jacobian"]),
               "fea_max_jacobian_ratio": r(worst["jac_ratio"]),
               "fea_max_aspect_ratio": r(worst["aspect"]),
               "fea_min_dihedral_deg": r(worst["min_dihedral"]),
               "fea_max_dihedral_deg": r(worst["max_dihedral"])}
    return {**figures, "fea_inverted": inverted, "elements": int(len(els)),
            "over_limit": over, "worst_at_m": where,
            "limits": [{"key": k, "label": lab, "op": op, "limit": lim, "source": src,
                        "measured": figures[k], "elements_over": over[k]} for k, lab, op, lim, src in LIMITS]}


__all__ = ["LIMITS", "element_measures", "measure"]
