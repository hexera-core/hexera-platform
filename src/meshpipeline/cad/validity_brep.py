# Responsibility: The clean contract (cad/validity) on exact CAD - a B-rep read by OpenCASCADE.
# Owns: how each rule is asked of a B-rep: closed (free edges, after sewing within a tight
# tolerance), valid (BRepCheck), tight tolerances, outward solids that enclose volume, no zero-area
# faces, and no crossings - found on a fine tessellation and confirmed on the exact faces.
# Boundaries: it reads the shape and never changes it (crossings are searched on a copy). The
# located free bounds and kernel statuses come from cad/repair/localize, the one place that walks
# them; places are in millimetres, the unit OpenCASCADE reads every CAD file into.
from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path

import numpy as np

from meshpipeline.cad.validity import (
    ERROR,
    FLOAT64_OFFSET_RATIO,
    INFO,
    MAX_PLACES,
    ZERO_VOLUME_RATIO,
    Finding,
    ValidityReport,
    _places,
    _r,
    _r1,
    verdict,
)

logger = logging.getLogger(__name__)

#: Free edges that sewing closes within this share of the part's diagonal are a file written
#: unsewn, not a gap: the part counts as closed (and says it was sewn).
SEW_TOLERANCE = 1e-4
#: Faces the sewing is tried on; a larger open model is reported open as it stands.
MAX_SEW_FACES = 20_000
#: An edge or vertex tolerance above this share of the diagonal papers over a gap.
LOOSE_TOLERANCE = 1e-3
#: A face whose area is below this share of the diagonal squared is a zero-area face.
DEGENERATE_AREA = 1e-12
#: A believable part, in millimetres: from a nanometre to 10,000 km across.
MIN_SIZE_MM, MAX_SIZE_MM = 1e-6, 1e10
#: The crossing search's tessellation: chord error as a share of the diagonal, angle in radians.
CROSS_DEFLECTION = 1e-3
CROSS_ANGLE = 0.5
#: Tessellated face pairs confirmed on the exact faces, and body pairs confirmed by their common
#: volume. Both are counts of work; past them the check says it did not finish.
MAX_FACE_PAIRS = 300
MAX_BODY_PAIRS = 60
#: Two bodies overlap when their common volume is above this share of the smaller one.
OVERLAP_SHARE = 1e-6
#: Kernel statuses that only restate a finding already reported (a free edge is the open shell).
_RESTATES_OPEN = {"NotClosed", "FreeEdge", "EmptyShell"}


def check_shape(shape, *, t0: float | None = None) -> ValidityReport:
    from OCP.BRepCheck import BRepCheck_Analyzer

    from meshpipeline.cad.repair.localize import _open_boundary_defects, _status_defects

    t0 = time.time() if t0 is None else t0
    findings: list[Finding] = []
    counts = _counts(shape)
    measured: dict = dict(counts)
    if counts["faces"] == 0:
        return verdict("brep", [Finding("empty", ERROR, "the CAD file holds no faces")], measured,
                       complete=True, t0=t0, units="mm")
    lo, hi = _bbox(shape)
    if lo is None or not (np.isfinite(lo).all() and np.isfinite(hi).all()):
        return verdict("brep", [Finding("non_finite", ERROR,
                                        "the part's extent is not a number (infinite or broken geometry)")],
                       measured, complete=True, t0=t0, units="mm")
    ext = hi - lo
    diag = float(np.linalg.norm(ext))
    measured.update(bbox_min=_r(lo), bbox_max=_r(hi), diag=_r1(diag))
    findings.extend(_plausible(lo, hi, diag))
    if diag <= 0.0:
        return verdict("brep", findings, measured, complete=True, t0=t0, units="mm")

    # CLOSED: no free edge, and no face outside a shell (free-bound analysis reads shells only, so
    # a file of loose faces - common in IGES - is sewn before it is judged), unless sewing within a
    # tight tolerance closes them
    work = shape
    loops = _open_boundary_defects(shape, diagonal=diag, interior=set())
    loose = _free_faces(shape)
    measured["faces_outside_shells"] = len(loose)
    if loops or loose:
        tol = SEW_TOLERANCE * diag
        sewn = _sew(shape, tol) if counts["faces"] <= MAX_SEW_FACES else None
        if sewn is not None:
            after = _open_boundary_defects(sewn, diagonal=diag, interior=set())
            still_loose = _free_faces(sewn)
        else:
            after, still_loose = loops, loose
        if sewn is not None and not after and not still_loose:
            work = sewn
            findings.append(Finding("sewn", INFO,
                                    f"the faces were written unsewn; they sew closed within "
                                    f"{tol:.3g} mm", len(loops) + len(loose),
                                    details={"tolerance_mm": _r1(tol)}))
        else:
            findings.append(_open_shell(after, still_loose, diag, sewn is not None, tol))
    # VALID: what the kernel attributes to an entity, by status
    valid = bool(BRepCheck_Analyzer(work, True).IsValid())
    measured["brep_valid"] = valid
    complete = True
    if not valid:
        located, walked_all = _status_defects(work, interior=set(), valid=False)
        complete = complete and walked_all
        by_status: dict[str, list] = {}
        for d in located:
            if d.kernel_status in _RESTATES_OPEN:
                continue
            by_status.setdefault(d.kernel_status, []).append(d)
        for status, ds in sorted(by_status.items()):
            pts = [d.location["centroid"] for d in ds if d.location.get("centroid")]
            findings.append(Finding("invalid_brep", ERROR,
                                    f"OpenCASCADE finds {len(ds)} {ds[0].entity_type}(s) invalid: "
                                    f"{status}", len(ds),
                                    _places(np.asarray(pts), diag) if pts else (),
                                    {"kernel_status": status}))
        if not by_status and not any(f.code == "open_shell" for f in findings):
            findings.append(Finding("invalid_brep", ERROR,
                                    "OpenCASCADE finds the B-rep invalid but names no face or edge"))
    findings.extend(_tolerance(work, diag, measured))
    findings.extend(_degenerate_faces(work, diag))
    findings.extend(_orientation(work, diag, measured))
    ok = _crossings(work, diag, findings, measured)
    return verdict("brep", findings, measured, complete=complete and ok, t0=t0, units="mm")


# ------------------------------------------------------------------------------- the rules ----
def _plausible(lo, hi, diag: float) -> list[Finding]:
    from meshpipeline.cad.validity import FLAT_RATIO

    out: list[Finding] = []
    if diag <= 0.0:
        return [Finding("implausible_coordinates", ERROR, "the part has no extent")]
    ext = hi - lo
    flat = [a for a in range(3) if ext[a] <= FLAT_RATIO * diag]
    if flat:
        out.append(Finding("flat", ERROR,
                           f"the geometry has no thickness along {'/'.join('xyz'[a] for a in flat)}: "
                           "a sheet, not a solid", len(flat)))
    if not (MIN_SIZE_MM <= diag <= MAX_SIZE_MM):
        out.append(Finding("implausible_size", ERROR,
                           f"the part is {diag:.3g} mm across - not a believable size for a real part",
                           details={"diag_mm": _r1(diag)}))
    offset = float(np.max(np.abs(np.r_[lo, hi])))
    if offset > FLOAT64_OFFSET_RATIO * diag:
        out.append(Finding("implausible_coordinates", ERROR,
                           f"the part sits {offset / diag:,.0f} of its own sizes from the origin",
                           1, ({"at": _r((lo + hi) / 2)},)))
    return out


def _open_shell(loops, loose: list, diag: float, tried_sewing: bool, tol: float) -> Finding:
    places = []
    for d in sorted(loops, key=lambda d: -float(d.measurements.get("boundary_length") or 0.0))[:MAX_PLACES]:
        loc = d.location or {}
        places.append({"at": loc.get("centroid"), "length": d.measurements.get("boundary_length"),
                       "edges": len(d.measurements.get("boundary_edges") or [])})
    for f in loose[:max(0, MAX_PLACES - len(places))]:
        if (c := _centre(f)) is not None:
            places.append({"at": c, "loose_face": True})
    biggest = places[0] if places else {}
    how = f" that no sewing within {tol:.3g} mm closes" if tried_sewing else ""
    what = []
    if loops:
        what.append(f"{len(loops)} free boundary(ies)")
    if loose:
        what.append(f"{len(loose)} face(s) joined to no shell")
    return Finding("open_shell", ERROR,
                   f"the surface is not closed: {' and '.join(what) or 'free edges'}{how}"
                   + (f"; the largest boundary runs {biggest['length']:.4g} mm round, at {biggest['at']}"
                      if biggest.get("length") else ""),
                   len(loops) + len(loose), tuple(places))


def _free_faces(shape) -> list:
    """Faces that belong to no shell: loose surfaces, whose edges free-bound analysis never sees."""
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SHELL
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    owners = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_FACE, TopAbs_SHELL, owners)
    return [owners.FindKey(i) for i in range(1, owners.Extent() + 1) if owners.FindFromIndex(i).Size() == 0]


def _tolerance(shape, diag: float, measured: dict) -> list[Finding]:
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_VERTEX
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    worst = 0.0
    loose: list = []
    for kind, tol_of in ((TopAbs_VERTEX, lambda s: BRep_Tool.Tolerance_s(TopoDS.Vertex_s(s))),
                         (TopAbs_EDGE, lambda s: BRep_Tool.Tolerance_s(TopoDS.Edge_s(s)))):
        ex = TopExp_Explorer(shape, kind)
        while ex.More():
            sub = ex.Current()
            ex.Next()
            try:
                t = float(tol_of(sub))
            except Exception:  # noqa: BLE001, S112 - an entity with no tolerance has none to report
                continue
            if t > worst:
                worst = t
            if t > LOOSE_TOLERANCE * diag:
                loose.append((t, sub))
    measured["max_tolerance_mm"] = _r1(worst)
    if not loose:
        return []
    loose.sort(key=lambda x: -x[0])
    places = tuple({"at": c, "tolerance_mm": _r1(t)} for t, s in loose[:MAX_PLACES]
                   if (c := _centre(s)) is not None)
    return [Finding("loose_tolerance", ERROR,
                    f"{len(loose)} edge(s)/vertex(es) carry a tolerance up to {worst:.3g} mm "
                    f"({worst / diag:.2g} of the part): a gap papered over", len(loose), places)]


def _degenerate_faces(shape, diag: float) -> list[Finding]:
    from OCP.TopAbs import TopAbs_FACE

    from meshpipeline.cad.repair.localize import _face_area, _indexed

    faces = _indexed(shape, TopAbs_FACE)
    bad = []
    for i in range(1, faces.Extent() + 1):
        f = faces.FindKey(i)
        if _face_area(f) <= DEGENERATE_AREA * diag * diag:
            bad.append(f)
    if not bad:
        return []
    pts = [c for f in bad if (c := _centre(f)) is not None]
    return [Finding("degenerate_faces", ERROR, f"{len(bad)} face(s) have zero area", len(bad),
                    _places(np.asarray(pts), diag) if pts else ())]


def _orientation(shape, diag: float, measured: dict) -> list[Finding]:
    """Every solid (and every closed shell outside a solid) encloses volume; every solid faces outward."""
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopExp import TopExp, TopExp_Explorer
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    bodies = []
    ex = TopExp_Explorer(shape, TopAbs_SOLID)
    while ex.More():
        bodies.append(ex.Current())
        ex.Next()
    owners = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_SHELL, TopAbs_SOLID, owners)
    ex = TopExp_Explorer(shape, TopAbs_SHELL)
    while ex.More():
        sh = ex.Current()
        ex.Next()
        if not owners.Contains(sh) or owners.FindFromKey(sh).Size() == 0:
            from OCP.BRep import BRep_Tool

            if BRep_Tool.IsClosed_s(sh):
                bodies.append(sh)
    inward, empty, total = [], [], 0.0
    for b in bodies:
        props = GProp_GProps()
        try:
            BRepGProp.VolumeProperties_s(b, props)
            v = float(props.Mass())
        except Exception:  # noqa: BLE001, S112 - a body whose volume cannot be taken is left to BRepCheck
            continue
        lo, hi = _bbox(b)
        box = float(np.prod(np.maximum(hi - lo, 0.0))) if lo is not None else 0.0
        total += abs(v)
        if abs(v) <= ZERO_VOLUME_RATIO * max(box, 1e-300):
            empty.append(b)
        elif v < 0 and b.ShapeType() == TopAbs_SOLID:
            # only a SOLID says which side is material; a closed shell (a surface model, or loose
            # faces sewn here) has no side of its own to be wrong about
            inward.append(b)
    measured.update(bodies=len(bodies), volume_mm3=_r1(total))
    out = []
    if inward:
        out.append(Finding("inside_out", ERROR, f"{len(inward)} solid(s) face inward (inside out)",
                           len(inward), tuple({"at": c} for b in inward[:MAX_PLACES]
                                              if (c := _centre(b)) is not None)))
    if empty:
        out.append(Finding("zero_volume", ERROR, f"{len(empty)} closed body(ies) enclose no volume",
                           len(empty), tuple({"at": c} for b in empty[:MAX_PLACES]
                                             if (c := _centre(b)) is not None)))
    return out


def _crossings(shape, diag: float, findings: list[Finding], measured: dict) -> bool:
    """No face passes through another of the same body; no body passes into another. Searched on
    a fine tessellation of a copy, each candidate confirmed on the exact geometry: a face pair by
    its true distance, a body pair by the volume the two share. Returns whether it finished."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
    from OCP.BRepMesh import BRepMesh_IncrementalMesh

    from meshpipeline.cad.crossings import find_crossings, nested_bodies
    from meshpipeline.cad.validity import MAX_VERIFIED_TRIANGLES

    try:
        work = BRepBuilderAPI_Copy(shape, True, False).Shape()
        BRepMesh_IncrementalMesh(work, CROSS_DEFLECTION * diag, False, CROSS_ANGLE, True)
        faces, face_of, body_of_face, pts, tris = _tessellation(work)
    except Exception as exc:  # noqa: BLE001 - a shape that will not tessellate is not verified
        findings.append(Finding("not_verified", ERROR,
                                f"the part could not be tessellated to search for crossings "
                                f"({type(exc).__name__})"))
        return False
    measured["check_triangles"] = int(len(tris))
    if len(tris) > MAX_VERIFIED_TRIANGLES:
        findings.append(Finding("not_verified", ERROR,
                                f"the part tessellates into {len(tris):,} triangles; the check verifies "
                                f"up to {MAX_VERIFIED_TRIANGLES:,}"))
        return False
    found = find_crossings(pts, tris)
    measured["crossing_pairs_tested"] = int(found.tested)
    complete = found.complete
    if not found.complete:
        findings.append(Finding("not_verified", ERROR,
                                "the surface is too dense to finish the crossing search within its budget"))
    fi, fj = face_of[found.pairs[:, 0]], face_of[found.pairs[:, 1]]
    mid = (pts[tris[found.pairs[:, 0]]].mean(axis=1) + pts[tris[found.pairs[:, 1]]].mean(axis=1)) / 2
    keep = fi != fj
    fp = np.unique(np.sort(np.c_[fi[keep], fj[keep]], axis=1), axis=0)
    adjacent = _adjacent_faces(work, faces) if len(fp) else set()
    within, between = [], set()
    # a body inside another's material, or sharing a face while running into it, crosses nothing:
    # read by the winding number of points just inside each body (crossings.nested_bodies)
    body_tri = body_of_face[face_of]
    C = pts[tris] - (pts.min(axis=0) + pts.max(axis=0)) / 2.0      # about the part, not the origin
    sv = np.einsum("ij,ij->i", C[:, 0], np.cross(C[:, 1], C[:, 2]))
    ok_b = body_tri >= 0
    vol = np.bincount(body_tri[ok_b], weights=sv[ok_b], minlength=int(body_tri.max()) + 1 if ok_b.any() else 0)
    nest = nested_bodies(pts, tris, body_tri, vol < 0)
    nested = {(min(k, j), max(k, j)) for k, j, _at in nest.found if j >= 0}
    if not nest.complete:
        complete = False
        findings.append(Finding("not_verified", ERROR,
                                "there are too many bodies inside each other's boxes to finish the "
                                "overlap search"))
    between |= nested
    for a, b in fp.tolist():
        ba, bb = body_of_face[a], body_of_face[b]
        if ba != bb and ba >= 0 and bb >= 0:
            between.add((min(ba, bb), max(ba, bb)))
        elif (a, b) not in adjacent:
            within.append((a, b))
    # within one body: the exact faces must meet
    real = []
    for a, b in within[:MAX_FACE_PAIRS]:
        if _faces_meet(faces[a], faces[b], diag):
            real.append((a, b))
    if len(within) > MAX_FACE_PAIRS and not real:
        complete = False
        findings.append(Finding("not_verified", ERROR,
                                f"{len(within)} face pairs may cross; the check confirms up to "
                                f"{MAX_FACE_PAIRS} and found none of those real"))
    if real:
        at = [mid[keep][(np.sort(np.c_[fi[keep], fj[keep]], axis=1) == np.array(p)).all(axis=1)].mean(axis=0)
              for p in real[:MAX_PLACES]]
        findings.append(Finding("self_intersection", ERROR,
                                f"the surface passes through itself: {len(real)} pair(s) of faces cross"
                                + ("+" if len(within) > MAX_FACE_PAIRS else ""),
                                len(real), _places(np.asarray(at), diag),
                                {"faces": [[int(a) + 1, int(b) + 1] for a, b in real[:20]],
                                 # the body (1-based, OpenCASCADE's solid order: an assembly's Part_k)
                                 "bodies": sorted({int(body_of_face[a]) + 1 for a, _b in real
                                                   if body_of_face[a] >= 0})}))
    # between bodies: they must share volume, not just touch
    bodies = _bodies(work)
    overlap: list[tuple[int, int, float | None]] = []
    unknown = 0
    pairs = sorted(between)
    for a, b in pairs[:MAX_BODY_PAIRS]:
        share = _common_share(bodies[a], bodies[b])
        if share is None and (a, b) in nested:
            overlap.append((a, b, None))        # shells: the winding number is the evidence
        elif share is None:
            unknown += 1
        elif share > OVERLAP_SHARE:
            overlap.append((a, b, share))
    if (len(pairs) > MAX_BODY_PAIRS or unknown) and not overlap:
        complete = False
        findings.append(Finding("not_verified", ERROR,
                                f"{len(pairs)} body pair(s) may overlap and {unknown} could not be "
                                f"measured; the check confirms up to {MAX_BODY_PAIRS}"))
    if overlap:
        findings.append(Finding("bodies_overlap", ERROR,
                                f"{len(overlap)} pair(s) of separate bodies pass into each other",
                                len(overlap),
                                tuple({"at": c, "bodies": [a + 1, b + 1],
                                       "shared_volume_share": None if s is None else _r1(s)}
                                      for a, b, s in overlap[:MAX_PLACES]
                                      if (c := _centre_common(bodies[a], bodies[b])) is not None)))
    elif between:
        findings.append(Finding("bodies_touch", INFO,
                                f"{len(between)} pair(s) of bodies touch without overlapping", len(between)))
    return complete


# ------------------------------------------------------------------------------- helpers ------
def _counts(shape) -> dict:
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    out = {}
    for name, kind in (("solids", TopAbs_SOLID), ("shells", TopAbs_SHELL), ("faces", TopAbs_FACE),
                       ("edges", TopAbs_EDGE)):
        m = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(shape, kind, m)
        out[name] = int(m.Extent())
    return out


def _bbox(shape):
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    try:
        BRepBndLib.AddOptimal_s(shape, box, False, False)
    except Exception:  # noqa: BLE001 - fall back to the quick box
        BRepBndLib.Add_s(shape, box)
    if box.IsVoid():
        return None, None
    x0, y0, z0, x1, y1, z1 = box.Get()
    return np.array([x0, y0, z0]), np.array([x1, y1, z1])


def _centre(sub) -> list[float] | None:
    lo, hi = _bbox(sub)
    return None if lo is None else _r((lo + hi) / 2)


def _sew(shape, tol: float):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing

    try:
        sewing = BRepBuilderAPI_Sewing(tol)
        sewing.Add(shape)
        sewing.Perform()
        out = sewing.SewedShape()
        return None if out is None or out.IsNull() else out
    except Exception as exc:  # noqa: BLE001 - a sewing that fails leaves the part as it is
        logger.info("clean check: sewing failed (%s)", exc)
        return None


def _tessellation(shape):
    """The tessellated faces as one welded triangle set (written by OpenCASCADE's own STL writer,
    which walks the faces in explorer order), with each triangle's face and each face's body."""
    from OCP.StlAPI import StlAPI_Writer
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopExp import TopExp, TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_IndexedMapOfShape

    from meshpipeline.cad.ingest.surface import read_stl

    fmap = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, fmap)
    faces = [fmap.FindKey(i) for i in range(1, fmap.Extent() + 1)]
    # the body a face belongs to: its solid, else its shell (a body of its own), else none
    bodies = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_SOLID, bodies)
    solid_of = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_FACE, TopAbs_SOLID, solid_of)
    shell_of = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_FACE, TopAbs_SHELL, shell_of)
    shells = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_SHELL, shells)
    n_solids = bodies.Extent()
    body_of_face = np.full(len(faces), -1, dtype=np.int64)
    for i, f in enumerate(faces):
        if solid_of.Contains(f) and solid_of.FindFromKey(f).Size():
            body_of_face[i] = bodies.FindIndex(solid_of.FindFromKey(f).First()) - 1
        elif shell_of.Contains(f) and shell_of.FindFromKey(f).Size():
            body_of_face[i] = n_solids + shells.FindIndex(shell_of.FindFromKey(f).First()) - 1
    # the writer emits each face OCCURRENCE in explorer order; count triangles the same way
    per = []
    ex = TopExp_Explorer(shape, TopAbs_FACE)
    from OCP.BRep import BRep_Tool

    while ex.More():
        f = TopoDS.Face_s(ex.Current())
        ex.Next()
        tri = BRep_Tool.Triangulation_s(f, TopLoc_Location())
        per.append((fmap.FindIndex(f) - 1, 0 if tri is None else int(tri.NbTriangles())))
    with tempfile.TemporaryDirectory(prefix="clean_tess_") as tmp:
        out = Path(tmp) / "t.stl"
        w = StlAPI_Writer()
        w.ASCIIMode = False
        if not w.Write(shape, str(out)):
            raise RuntimeError("the tessellation could not be written")
        mesh = read_stl(out)
    n = mesh.n_triangles
    if sum(c for _f, c in per) != n:
        raise RuntimeError("the tessellation does not match its faces")
    face_of = np.repeat(np.array([f for f, _c in per], dtype=np.int64),
                        np.array([c for _f, c in per], dtype=np.int64))
    pts, inv = np.unique(mesh.corners().reshape(-1, 3), axis=0, return_inverse=True)
    tris = np.asarray(inv, dtype=np.int64).reshape(-1, 3)
    good = (tris[:, 0] != tris[:, 1]) & (tris[:, 1] != tris[:, 2]) & (tris[:, 0] != tris[:, 2])
    return faces, face_of[good], body_of_face, pts, tris[good]


def _adjacent_faces(shape, faces) -> set:
    """Face pairs (0-based, a < b) that share a vertex: neighbours, which meet by design."""
    from OCP.TopAbs import TopAbs_FACE, TopAbs_VERTEX
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_IndexedMapOfShape

    fmap = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, fmap)
    vf = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_VERTEX, TopAbs_FACE, vf)
    out: set = set()
    for i in range(1, vf.Extent() + 1):
        ids = sorted({fmap.FindIndex(f) - 1 for f in vf.FindFromIndex(i)})
        for x in range(len(ids)):
            for y in range(x + 1, len(ids)):
                out.add((ids[x], ids[y]))
    return out


def _faces_meet(fa, fb, diag: float) -> bool:
    from OCP.BRep import BRep_Tool
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.TopoDS import TopoDS

    try:
        d = BRepExtrema_DistShapeShape(fa, fb)
        if not d.IsDone():
            return True                       # cannot be cleared: counted, never waved through
        tol = max(1e-7 * diag, BRep_Tool.Tolerance_s(TopoDS.Face_s(fa)) + BRep_Tool.Tolerance_s(TopoDS.Face_s(fb)))
        return float(d.Value()) <= tol
    except Exception:  # noqa: BLE001
        return True


def _bodies(shape) -> list:
    from OCP.TopAbs import TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    solids = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_SOLID, solids)
    shells = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_SHELL, shells)
    return ([solids.FindKey(i) for i in range(1, solids.Extent() + 1)]
            + [shells.FindKey(i) for i in range(1, shells.Extent() + 1)])


def _volume(s) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(s, props)
    return float(props.Mass())


def _common_share(a, b) -> float | None:
    """The volume two bodies share, as a share of the smaller one; None when it cannot be told."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    from OCP.TopAbs import TopAbs_SOLID

    if a.ShapeType() != TopAbs_SOLID or b.ShapeType() != TopAbs_SOLID:
        return None
    try:
        op = BRepAlgoAPI_Common(a, b)
        if not op.IsDone():
            return None
        small = min(abs(_volume(a)), abs(_volume(b)))
        return abs(_volume(op.Shape())) / small if small > 0 else None
    except Exception:  # noqa: BLE001
        return None


def _centre_common(a, b) -> list[float] | None:
    la, ha = _bbox(a)
    lb, hb = _bbox(b)
    if la is None or lb is None:
        return None
    return _r((np.maximum(la, lb) + np.minimum(ha, hb)) / 2)



