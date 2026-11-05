# Responsibility: Measure a tetrahedral mesh the way a finite-volume CFD solver sees it - face non-orthogonality and skewness, as OpenFOAM's checkMesh defines them.
# Boundaries: pure numpy over node coordinates and tetrahedra; it reads no file and knows no gate. The gmsh driver (run inside the mesh image) calls it.
# Collaborates with: engines/gmsh/driver.py (writes the numbers into quality.json), engines/gmsh/gates.py (the CFD quality bar).
"""FACE QUALITY OF A TET MESH, IN A CFD SOLVER'S TERMS.

SICN (gmsh's element measure) is the conditioning of one element - the bar an FE stiffness matrix
needs. A finite-volume solver integrates over FACES: what it needs is the angle between the line
joining two cell centres and the face between them (non-orthogonality) and how far that line
misses the face centre (skewness). snappyHexMesh and cfMesh meshes are judged on those numbers;
a tet mesh delivered for CFD is measured the same way here, with checkMesh's own definitions
(src/OpenFOAM/meshes/primitiveMesh/primitiveMeshCheck: faceOrthogonality, faceSkewness), so
the numbers are comparable across engines.

On the lab's 13 gmsh meshes (2026-10-05) SICN did not track them: a wing at SICN 0.110 (passing
the 0.1 floor) had 154 faces over 70 degrees and a worst of 81.0; a volute at 0.074 (failing it)
had 10 and a worst of 80.2.
"""
from __future__ import annotations

import numpy as np

#: checkMesh calls a face "severely" non-orthogonal above this (primitiveMesh::nonOrthThreshold_).
SEVERE_NON_ORTHO_DEG = 70.0
#: checkMesh's skewness warning threshold (primitiveMesh::skewThreshold_).
HIGH_SKEW = 4.0

_FACE_OF_TET = np.asarray([[1, 2, 3], [0, 3, 2], [0, 1, 3], [0, 2, 1]], dtype=np.int64)


def _keys(tri: np.ndarray, n_nodes: int) -> np.ndarray:
    """One integer per triangle, the same for the same three nodes in any order."""
    s = np.sort(tri, axis=1).astype(np.int64)
    if n_nodes < (1 << 21):
        return (s[:, 0] << 42) | (s[:, 1] << 21) | s[:, 2]
    return np.unique(s, axis=0, return_inverse=True)[1].ravel().astype(np.int64)


def face_quality(points, tets) -> dict:
    """Non-orthogonality (degrees) of every internal face and skewness of every face of a linear
    tet mesh (corner nodes only), summarised: max and mean non-orthogonality, faces over
    SEVERE_NON_ORTHO_DEG, max skewness and faces over HIGH_SKEW, and the face counts. {} for an
    empty mesh."""
    X = np.asarray(points, dtype=float)
    T = np.asarray(tets, dtype=np.int64)[:, :4]
    if len(T) == 0:
        return {}
    cc = X[T].mean(axis=1)                                        # tet centroids
    tri = T[:, _FACE_OF_TET].reshape(-1, 3)                       # 4 faces per tet
    owner = np.repeat(np.arange(len(T)), 4)
    a, b, c = X[tri[:, 0]], X[tri[:, 1]], X[tri[:, 2]]
    cf = (a + b + c) / 3.0
    S = 0.5 * np.cross(b - a, c - a)
    # every area vector pointing out of its own tet
    flip = np.einsum("ij,ij->i", S, cf - cc[owner]) < 0.0
    S[flip] *= -1.0
    keys = _keys(tri, len(X))
    order = np.argsort(keys, kind="stable")
    ks = keys[order]
    pair = np.flatnonzero(ks[1:] == ks[:-1])                      # internal faces: key twice
    f_own, f_nei = order[pair], order[pair + 1]
    internal = np.zeros(len(tri), dtype=bool)
    internal[f_own] = True
    internal[f_nei] = True
    # INTERNAL faces: owner -> neighbour
    P, N = owner[f_own], owner[f_nei]
    d = cc[N] - cc[P]
    Sf = S[f_own]
    magS = np.linalg.norm(Sf, axis=1) + 1e-300
    magd = np.linalg.norm(d, axis=1) + 1e-300
    cosang = np.clip(np.einsum("ij,ij->i", Sf, d) / (magS * magd), -1.0, 1.0)
    non_ortho = np.degrees(np.arccos(cosang))
    # skewness, faceSkewness: how far the centre-to-centre line misses the face centre, over the
    # distance from the face centre to its edge in that direction (at least 0.2 |d|)
    cpf = cf[f_own] - cc[P]
    t = np.einsum("ij,ij->i", Sf, cpf) / (np.einsum("ij,ij->i", Sf, d) + 1e-300)
    sv = cpf - t[:, None] * d
    skew_in = _normalised(sv, tri[f_own], X, cf[f_own], 0.2 * magd)
    # BOUNDARY faces: the cell centre's offset along the face (OpenFOAM's boundary skewness)
    bnd = np.flatnonzero(~internal)
    skew_bd = np.zeros(0)
    if len(bnd):
        n = S[bnd] / (np.linalg.norm(S[bnd], axis=1, keepdims=True) + 1e-300)
        cpf_b = cf[bnd] - cc[owner[bnd]]
        dn = n * np.einsum("ij,ij->i", n, cpf_b)[:, None]
        sv_b = cpf_b - dn
        skew_bd = _normalised(sv_b, tri[bnd], X, cf[bnd], 0.2 * np.linalg.norm(dn, axis=1))
    skew = np.concatenate([skew_in, skew_bd])
    return {
        "max_non_ortho": round(float(non_ortho.max()), 4) if len(non_ortho) else 0.0,
        "mean_non_ortho": round(float(non_ortho.mean()), 4) if len(non_ortho) else 0.0,
        "non_ortho_severe_faces": int((non_ortho > SEVERE_NON_ORTHO_DEG).sum()),
        "max_skewness": round(float(skew.max()), 4) if len(skew) else 0.0,
        "skew_faces": int((skew > HIGH_SKEW).sum()),
        "internal_faces": int(len(f_own)),
        "boundary_faces": int(len(bnd)),
    }


def _normalised(sv: np.ndarray, tri: np.ndarray, X: np.ndarray, cf: np.ndarray,
                floor: np.ndarray) -> np.ndarray:
    mag = np.linalg.norm(sv, axis=1)
    hat = sv / (mag[:, None] + 1e-300)
    fd = floor + 1e-300
    for k in range(3):
        fd = np.maximum(fd, np.abs(np.einsum("ij,ij->i", hat, X[tri[:, k]] - cf)))
    return mag / fd


__all__ = ["HIGH_SKEW", "SEVERE_NON_ORTHO_DEG", "face_quality"]
