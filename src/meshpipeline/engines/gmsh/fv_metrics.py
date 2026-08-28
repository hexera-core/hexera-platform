# Responsibility: Measure a gmsh tet mesh with the finite-volume metrics OpenFOAM's checkMesh uses.
# Boundaries: pure geometry - numpy in, numbers out. No gmsh calls except the one extractor
#   that reads a live model; no thresholds (the caller passes its bars).
#
# VENDORED, with attribution: the formula implementations below are copied from the calibrated
# quality-audit scorer at /home/areen/quality_audit/scorer/meshscore/ (metrics.py +
# msh_volume.py), which matches OpenFOAM v2412 primitiveMesh / primitiveMeshTools and is
# calibrated against real checkMesh runs. They are vendored VERBATIM (same arithmetic, same
# chunking, same conventions) so this engine's gate and the fleet audit measure IDENTICALLY -
# tests/unit/engines/test_gmsh_fv_quality.py holds the two to 1e-9 agreement. The scorer's
# convention discoveries are load-bearing:
#   - checkMesh's printed "average" non-orthogonality is acos(mean(cos theta)), NOT the mean angle
#   - faceSkewness floors its normalisation at 0.2|d| internal and 0.4|d| BOUNDARY
#   - tet10 meshes are measured on the corner-node (linear) geometry, which is what a
#     finite-volume solver actually sees (matches gmshToFoam)
# Do not "improve" a formula here without re-running the parity test against the scorer.
from __future__ import annotations

from typing import Any

import numpy as np

ROOTVSMALL = 1.0e-18

_CHUNK_FACES = 1_500_000

#: Faces of a positively-oriented tet (v0..v3) with OUTWARD normals (meshscore/msh_volume.py).
_TET_FACES = np.array([[0, 2, 1], [0, 1, 3], [1, 2, 3], [0, 3, 2]])

#: gmsh 3D element types this module can measure: tet4 and tet10 (corner nodes only).
_TET_NODES_PER = {4: 4, 11: 10}


def signed_tet_vols(pts: np.ndarray, tets: np.ndarray) -> np.ndarray:
    a = pts[tets[:, 1]] - pts[tets[:, 0]]
    b = pts[tets[:, 2]] - pts[tets[:, 0]]
    c = pts[tets[:, 3]] - pts[tets[:, 0]]
    return np.einsum("ij,ij->i", np.cross(a, b), c) / 6.0


def _face_geometry_block(points, face_flat, face_off):
    """Fan decomposition on one contiguous block (meshscore/metrics.py, verbatim)."""
    sizes = np.diff(face_off)
    pv = points[face_flat]                                  # (E,3) vertex per slot
    fc_est = np.add.reduceat(pv, face_off[:-1]) / sizes[:, None]

    nxt = np.arange(1, len(face_flat) + 1)
    nxt[face_off[1:] - 1] = face_off[:-1]                   # wrap last -> first
    p1 = pv
    p2 = points[face_flat[nxt]]
    del nxt
    fce = np.repeat(fc_est, sizes, axis=0)

    n = np.cross(p2 - p1, fce - p1)
    c = p1 + p2 + fce
    del p2, fce
    a = np.linalg.norm(n, axis=1)

    sumN = np.add.reduceat(n, face_off[:-1])
    sumA = np.add.reduceat(a, face_off[:-1])
    sumAc = np.add.reduceat(a[:, None] * c, face_off[:-1])
    del n, c, a, pv

    fAreas = 0.5 * sumN
    with np.errstate(invalid="ignore", divide="ignore"):
        fCtrs = sumAc / (3.0 * sumA[:, None])
    bad = sumA < ROOTVSMALL
    if bad.any():
        fCtrs[bad] = fc_est[bad]
    # OpenFOAM special-cases triangles for exactness
    tri = sizes == 3
    if tri.any():
        i0 = face_flat[face_off[:-1][tri]]
        i1 = face_flat[face_off[:-1][tri] + 1]
        i2 = face_flat[face_off[:-1][tri] + 2]
        fCtrs[tri] = (points[i0] + points[i1] + points[i2]) / 3.0
        fAreas[tri] = 0.5 * np.cross(points[i1] - points[i0], points[i2] - points[i0])
    return fCtrs, fAreas


def face_geometry(points: np.ndarray, face_flat: np.ndarray, face_off: np.ndarray):
    """Face centres and area vectors, OpenFOAM fan decomposition (meshscore/metrics.py)."""
    nf = len(face_off) - 1
    if nf <= _CHUNK_FACES:
        return _face_geometry_block(points, face_flat, face_off)
    fCtrs = np.empty((nf, 3))
    fAreas = np.empty((nf, 3))
    for f0 in range(0, nf, _CHUNK_FACES):
        f1 = min(f0 + _CHUNK_FACES, nf)
        e0, e1 = face_off[f0], face_off[f1]
        fc, fa = _face_geometry_block(points, face_flat[e0:e1],
                                      face_off[f0:f1 + 1] - e0)
        fCtrs[f0:f1] = fc
        fAreas[f0:f1] = fa
    return fCtrs, fAreas


def cell_geometry(fCtrs, fAreas, owner, neighbour, n_cells):
    """Cell centres and volumes, OpenFOAM pyramid decomposition (meshscore/metrics.py)."""
    ni = len(neighbour)
    nfc = np.bincount(owner, minlength=n_cells) + \
        np.bincount(neighbour, minlength=n_cells)

    cEst = np.zeros((n_cells, 3))
    for k in range(3):
        cEst[:, k] = (np.bincount(owner, weights=fCtrs[:, k], minlength=n_cells) +
                      np.bincount(neighbour, weights=fCtrs[:ni, k], minlength=n_cells))
    cEst /= np.maximum(nfc, 1)[:, None]

    # owner side: outward normal is +fAreas
    d_own = fCtrs - cEst[owner]
    pyr3_own = np.einsum("ij,ij->i", fAreas, d_own)
    pc_own = 0.75 * fCtrs + 0.25 * cEst[owner]
    # neighbour side: outward normal is -fAreas
    d_nei = fCtrs[:ni] - cEst[neighbour]
    pyr3_nei = -np.einsum("ij,ij->i", fAreas[:ni], d_nei)
    pc_nei = 0.75 * fCtrs[:ni] + 0.25 * cEst[neighbour]

    cVols3 = np.bincount(owner, weights=pyr3_own, minlength=n_cells) + \
        np.bincount(neighbour, weights=pyr3_nei, minlength=n_cells)
    cCtrs = np.zeros((n_cells, 3))
    for k in range(3):
        cCtrs[:, k] = (np.bincount(owner, weights=pyr3_own * pc_own[:, k],
                                   minlength=n_cells) +
                       np.bincount(neighbour, weights=pyr3_nei * pc_nei[:, k],
                                   minlength=n_cells))
    with np.errstate(invalid="ignore", divide="ignore"):
        cCtrs /= cVols3[:, None]
    tiny = np.abs(cVols3) <= ROOTVSMALL
    if tiny.any():
        cCtrs[tiny] = cEst[tiny]
    cVols = cVols3 / 3.0
    return cCtrs, cVols, nfc


def non_orthogonality(cCtrs, fCtrs, fAreas, owner, neighbour) -> np.ndarray:
    """Internal-face non-orthogonality angle in degrees (meshscore/metrics.py, verbatim)."""
    ni = len(neighbour)
    d = cCtrs[neighbour] - cCtrs[owner[:ni]]
    s = fAreas[:ni]
    num = np.einsum("ij,ij->i", d, s)
    den = np.linalg.norm(d, axis=1) * np.linalg.norm(s, axis=1) + ROOTVSMALL
    cosang = np.clip(num / den, -1.0, 1.0)
    return np.degrees(np.arccos(cosang))


def skewness(points, face_flat, face_off, fCtrs, fAreas, cCtrs, owner, neighbour) -> np.ndarray:
    """OpenFOAM primitiveMeshTools::faceSkewness for all faces (meshscore/metrics.py, verbatim).

    Internal faces use d = C_nei - C_own; boundary faces use the wall-normal
    projection of C_face - C_own, with the normalisation floored at 0.2|d| internal
    and 0.4|d| boundary (checkMesh-verified). Returns (F,) skewness.
    """
    nf = len(face_off) - 1
    ni = len(neighbour)
    skew = np.empty(nf)

    own_c = cCtrs[owner]
    Cpf = fCtrs - own_c

    d = np.empty((nf, 3))
    d[:ni] = cCtrs[neighbour] - own_c[:ni]
    # boundary: d = n_hat * (n_hat & Cpf)
    nrm = fAreas[ni:]
    nmag = np.linalg.norm(nrm, axis=1)
    nhat = nrm / (nmag[:, None] + ROOTVSMALL)
    d[ni:] = nhat * np.einsum("ij,ij->i", nhat, Cpf[ni:])[:, None]

    sf_cpf = np.einsum("ij,ij->i", fAreas, Cpf)
    sf_d = np.einsum("ij,ij->i", fAreas, d)
    sv = Cpf - (sf_cpf / (sf_d + ROOTVSMALL))[:, None] * d
    svmag = np.linalg.norm(sv, axis=1)
    svHat = sv / (svmag[:, None] + ROOTVSMALL)

    fd = np.empty(nf)
    for f0 in range(0, nf, _CHUNK_FACES):
        f1 = min(f0 + _CHUNK_FACES, nf)
        e0, e1 = face_off[f0], face_off[f1]
        off = face_off[f0:f1 + 1] - e0
        sizes = np.diff(off)
        pv = points[face_flat[e0:e1]]
        fce = np.repeat(fCtrs[f0:f1], sizes, axis=0)
        sve = np.repeat(svHat[f0:f1], sizes, axis=0)
        proj = np.abs(np.einsum("ij,ij->i", sve, pv - fce))
        fd[f0:f1] = np.maximum.reduceat(proj, off[:-1])
        del pv, fce, sve, proj
    dmag = np.linalg.norm(d, axis=1)
    floor = np.empty(nf)
    floor[:ni] = 0.2 * dmag[:ni]
    floor[ni:] = 0.4 * dmag[ni:]
    fd = np.maximum(fd, floor + ROOTVSMALL)

    skew[:] = svmag / fd
    return skew


def tet_face_topology(pts: np.ndarray, tets: np.ndarray):
    """Face-addressed topology of a tet mesh: internal faces first, then boundary.

    Adapted from meshscore/msh_volume.py::build_from_tets with an empty patch table (this
    module measures geometry; it does not need the boundary named). The orientation fix,
    face matching and internal-face ordering are kept verbatim so the arrays agree with the
    scorer's element-for-element.
    Returns (face_flat, face_off, owner, neighbour) with owner < neighbour on internal faces.
    """
    sv = signed_tet_vols(pts, tets)
    neg = sv < 0
    tets = tets.copy()
    if neg.any():
        tets[neg] = tets[neg][:, [0, 1, 3, 2]]

    T = len(tets)
    tri = tets[:, _TET_FACES.T].transpose(0, 2, 1).reshape(4 * T, 3)
    key = np.sort(tri, axis=1)
    order = np.lexsort((key[:, 2], key[:, 1], key[:, 0]))
    ks = key[order]
    new_grp = np.any(np.diff(ks, axis=0) != 0, axis=1)
    grp_of_sorted = np.concatenate([[0], np.cumsum(new_grp)])
    n_unique = int(grp_of_sorted[-1]) + 1
    grp_count = np.bincount(grp_of_sorted, minlength=n_unique)
    if grp_count.max() > 2:
        raise ValueError("a triangle is shared by >2 tets - broken mesh")

    starts = np.searchsorted(grp_of_sorted, np.arange(n_unique))
    slot_first = order[starts]                    # slot index (t*4 + f)
    internal = grp_count == 2
    slot_second = order[np.minimum(starts + 1, len(order) - 1)]

    int_ids = np.where(internal)[0]
    bnd_ids = np.where(~internal)[0]

    o = slot_first[int_ids] // 4
    n = slot_second[int_ids] // 4
    swap = o > n
    slot = np.where(swap, slot_second[int_ids], slot_first[int_ids])
    o2 = np.where(swap, n, o)
    n2 = np.where(swap, o, n)

    oo = np.lexsort((n2, o2))
    int_slots = slot[oo]
    int_owner = o2[oo]
    int_nei = n2[oo]

    bnd_slots = slot_first[bnd_ids]
    bnd_owner = bnd_slots // 4

    all_slots = np.concatenate([int_slots, bnd_slots])
    face_flat = tri[all_slots].reshape(-1)
    nf = len(all_slots)
    face_off = np.arange(0, 3 * nf + 1, 3, dtype=np.int64)
    owner = np.concatenate([int_owner, bnd_owner]).astype(np.int64)
    neighbour = int_nei.astype(np.int64)
    return face_flat, face_off, owner, neighbour


def fv_summary(pts: np.ndarray, tets: np.ndarray) -> dict:
    """The FV-relevant quality numbers of a tet mesh, in checkMesh conventions.

    Keys follow the snappy path's quality vocabulary (max_non_ortho / max_skewness) so the
    shared run_mesh tool, criteria registry and manifest report card read one language.
    """
    face_flat, face_off, owner, neighbour = tet_face_topology(pts, tets)
    n_cells = len(tets)
    fCtrs, fAreas = face_geometry(pts, face_flat, face_off)
    cCtrs, _cVols, _ = cell_geometry(fCtrs, fAreas, owner, neighbour, n_cells)
    no = non_orthogonality(cCtrs, fCtrs, fAreas, owner, neighbour)
    sk = skewness(pts, face_flat, face_off, fCtrs, fAreas, cCtrs, owner, neighbour)
    ni = len(neighbour)
    # checkMesh's printed "average" is acos(mean(cos theta)), NOT the mean angle
    avg = (float(np.degrees(np.arccos(min(1.0, float(np.cos(np.radians(no)).mean())))))
           if len(no) else 0.0)
    return {
        "max_non_ortho": round(float(no.max()), 4) if len(no) else 0.0,
        "avg_non_ortho": round(avg, 4),
        "non_ortho_over_65": int(np.count_nonzero(no > 65.0)),
        "non_ortho_over_70": int(np.count_nonzero(no > 70.0)),
        "max_skewness": round(float(sk.max()), 4) if len(sk) else 0.0,
        "max_skewness_internal": round(float(sk[:ni].max()), 4) if ni else 0.0,
        "max_skewness_boundary": round(float(sk[ni:].max()), 4) if len(sk) > ni else 0.0,
        "fv_internal_faces": ni,
    }


def extract_tets(gmsh: Any) -> tuple[np.ndarray, np.ndarray]:
    """Corner-node points and tet connectivity of the CURRENT gmsh model.

    Adapted from meshscore/msh_volume.py::load_msh_raw: tet4 (type 4) and tet10 (type 11,
    corner nodes only - the linear geometry a finite-volume solver actually sees, matching
    gmshToFoam), compacted to the corner nodes actually used. Any other 3D element type
    raises (fail loudly rather than mis-measure).
    """
    node_tags, coords, _ = gmsh.model.mesh.getNodes()
    node_tags = np.asarray(node_tags, dtype=np.int64)
    pts = np.asarray(coords, dtype=np.float64).reshape(-1, 3)
    idx = np.full(int(node_tags.max()) + 1, -1, dtype=np.int64)
    idx[node_tags] = np.arange(len(node_tags))

    etypes, _, enodes = gmsh.model.mesh.getElements(3)
    unsupported = [int(t) for t in etypes if int(t) not in _TET_NODES_PER]
    if unsupported or not len(etypes):
        raise ValueError(
            f"3D element types {[int(t) for t in etypes]} - only tet4/tet10 are measurable "
            "on the gmsh FV path (fail loudly rather than mis-measure)")
    parts = []
    for tt, nn in zip(etypes, enodes):
        npn = _TET_NODES_PER[int(tt)]
        parts.append(np.asarray(nn, dtype=np.int64).reshape(-1, npn)[:, :4])
    tets = idx[np.vstack(parts)]
    if (tets < 0).any():
        raise ValueError("tet references an unknown node tag")

    used = np.unique(tets)
    remap = np.full(len(pts), -1, dtype=np.int64)
    remap[used] = np.arange(len(used))
    return pts[used], remap[tets]


def measure_model(gmsh: Any) -> dict:
    """fv_summary of the current gmsh model's 3D mesh."""
    pts, tets = extract_tets(gmsh)
    return fv_summary(pts, tets)


def fv_verdict(quality: dict, *, nonortho_hard: float, skew_internal_hard: float,
               skew_boundary_hard: float) -> tuple[bool, str]:
    """Judge a quality report against the hard FV bars. FAIL-CLOSED: a missing metric fails.

    Returns (ok, why) - `why` carries the measured numbers and the concrete fix so the
    rejection feeds the builder retry loop with something actionable.
    """
    keys = ("max_non_ortho", "max_skewness_internal", "max_skewness_boundary")
    missing = [k for k in keys if quality.get(k) is None]
    if missing:
        return False, (
            f"FV quality metrics {missing} are missing from the quality report - the mesh was "
            "never measured against the finite-volume bars (fail-closed). Run run_mesh again; "
            "if this recurs the mesh contains non-tet 3D elements the FV path cannot measure.")
    problems: list[str] = []
    no = float(quality["max_non_ortho"])
    if no > nonortho_hard:
        n70 = quality.get("non_ortho_over_70")
        problems.append(
            f"max face non-orthogonality {no:.1f} deg exceeds the {nonortho_hard:g} deg severe "
            f"bar (checkMesh convention; {n70 if n70 is not None else '?'} face(s) over 70 deg)")
    si = float(quality["max_skewness_internal"])
    if si > skew_internal_hard:
        problems.append(
            f"max internal-face skewness {si:.2f} exceeds the {skew_internal_hard:g} bar "
            "(checkMesh maxInternalSkewness)")
    sb = float(quality["max_skewness_boundary"])
    if sb > skew_boundary_hard:
        problems.append(
            f"max boundary-face skewness {sb:.2f} exceeds the {skew_boundary_hard:g} bar "
            "(checkMesh maxBoundarySkewness)")
    if problems:
        return False, ("; ".join(problems)
                       + ". Fix in gmsh_spec.json: keep optimize=true, reduce size.value "
                         "(finer elements resolve the distorted region) or raise "
                         "curvature_nodes near curved features, then run_mesh again - the "
                         "driver's optimizer ladder re-runs automatically.")
    return True, ""
