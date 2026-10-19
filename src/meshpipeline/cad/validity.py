# Responsibility: Decide whether an uploaded geometry is CLEAN - the machine check behind the
# promise "any clean geometry meshes". A file this check calls clean and the product then fails to
# mesh is a bug in the product, never the user's problem.
# Owns: what "clean" means (the rules below), the verdict, and the reasons - each with where on
# the part it is. One implementation for every format: exact CAD (STEP, IGES, BREP and what the
# ingest converts to STEP) is judged on its B-rep (cad/validity_brep), every triangle format (STL,
# OBJ, PLY, VTP, GLB, 3MF, ...) on its triangles as uploaded.
# Boundaries: it measures and reports. It repairs nothing (CAD repair is its own project),
# refuses nothing and never decides for the user: the geometry check stores the verdict beside its
# other facts. Deterministic: the same bytes give the same verdict on any machine, and every limit
# is a count of work, never a clock.
from __future__ import annotations

import logging
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

#: Bumped whenever a rule changes, so stored verdicts say which contract they were judged by.
CHECK_VERSION = "clean-1"

# THE CONTRACT. A geometry is clean when every one of these holds; each failure is a reason with
# its place on the part.
#
#   readable      the file reads, and holds a surface
#   finite        every coordinate is a number
#   plausible     the part has extent in all three directions (a solid, not a sheet), sits close
#                 enough to the origin that the file's own number precision can still draw it,
#                 and (exact CAD, whose unit is known) is between a nanometre and 10,000 km across
#   closed        every surface piece is closed: no edge belongs to one face only (a triangle
#                 file), no free edge that sewing within a tight tolerance cannot close (CAD)
#   manifold      no edge is shared by more than two faces, no two surfaces meet at a single corner
#   oriented      neighbouring faces agree which side is out, and every closed piece faces outward
#                 (a cavity inside another piece faces in, as it should)
#   solid         every closed piece encloses volume: no closed double-sided sheets
#   not degenerate  no zero-area faces, no face written twice
#   no crossings  the surface never passes through itself, and separate bodies never pass into
#                 each other (touching is allowed; overlapping is not)
#   valid B-rep   (CAD) OpenCASCADE's BRepCheck finds nothing wrong, and no edge or vertex
#                 carries a tolerance that papers over a gap (more than LOOSE_TOLERANCE of the part)
#   verified      the file was small enough to check completely; a file the check could not finish
#                 is not called clean

#: A 32-bit file (STL) holds about 7 significant digits. A part sitting more than this many of its
#: own sizes from the origin keeps under ~1/1000 of its size as resolution: thin walls collapse.
FLOAT32_OFFSET_RATIO = 1e4
#: A 64-bit format reaches the same limit only this far out.
FLOAT64_OFFSET_RATIO = 1e10
#: A part thinner than this share of its size along any axis is a sheet, not a solid.
FLAT_RATIO = 1e-9
#: Twice a triangle's area over its longest edge squared - its height relative to its length.
#: Below this the triangle is a line or a point: corners collinear or collapsed.
DEGENERATE_RATIO = 1e-10
#: A closed piece enclosing less than this share of its box's volume encloses nothing.
ZERO_VOLUME_RATIO = 1e-9
#: Triangles the check verifies completely; a larger file is "not verified" (stated, never guessed).
MAX_VERIFIED_TRIANGLES = 4_000_000
#: Places listed per reason (the count says how many there are in all).
MAX_PLACES = 10
#: Faces closer than this many steps of the file's own number precision count as touching.
PRECISION_STEPS = 4

ERROR, WARNING, INFO = "error", "warning", "info"


@dataclass(frozen=True)
class Finding:
    """One reason: what is wrong, how many times, and where (the largest or worst places first)."""

    code: str
    severity: str
    message: str
    count: int = 1
    places: tuple[dict, ...] = ()
    details: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        out = {"code": self.code, "severity": self.severity, "message": self.message,
               "count": int(self.count)}
        if self.places:
            out["places"] = [dict(p) for p in self.places]
        if self.details:
            out["details"] = dict(self.details)
        return out


@dataclass(frozen=True)
class ValidityReport:
    """The verdict. `clean` is True only when no error was found AND the check ran to the end."""

    clean: bool
    kind: str                       # "brep" | "triangles" | "unchecked"
    findings: tuple[Finding, ...]
    measured: dict
    complete: bool
    seconds: float = 0.0
    units: str = ""                 # what the places' coordinates are in
    version: str = CHECK_VERSION

    @property
    def reasons(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == ERROR)

    def summary(self) -> str:
        if self.kind == "unchecked":
            return self.findings[0].message if self.findings else "The geometry was not checked."
        if self.clean:
            extra = [f.message for f in self.findings if f.severity == INFO][:1]
            return "The geometry is clean." + (f" {extra[0]}" if extra else "")
        first = self.reasons[0].message if self.reasons else "The check did not finish."
        more = len(self.reasons) - 1
        return (f"The geometry is not clean: {first}"
                + (f" ({more} more reason{'s' if more > 1 else ''})" if more > 0 else ""))

    def as_dict(self) -> dict:
        return {"clean": bool(self.clean), "kind": self.kind, "version": self.version,
                "complete": bool(self.complete), "summary": self.summary(),
                "reasons": [f.as_dict() for f in self.findings if f.severity == ERROR],
                "warnings": [f.as_dict() for f in self.findings if f.severity == WARNING],
                "notes": [f.as_dict() for f in self.findings if f.severity == INFO],
                "measured": dict(self.measured), "units": self.units,
                "seconds": round(float(self.seconds), 2)}


def verdict(kind: str, findings: list[Finding], measured: dict, *, complete: bool, t0: float,
            units: str) -> ValidityReport:
    errors = [f for f in findings if f.severity == ERROR]
    if not complete and not any(f.code == "not_verified" for f in errors):
        findings = findings + [Finding("not_verified", ERROR,
                                       "the check could not finish on this file, so it is not "
                                       "certified clean")]
    order = {ERROR: 0, WARNING: 1, INFO: 2}
    findings = sorted(findings, key=lambda f: order.get(f.severity, 3))
    clean = complete and not any(f.severity == ERROR for f in findings)
    return ValidityReport(clean=clean, kind=kind, findings=tuple(findings), measured=measured,
                          complete=complete, seconds=time.time() - t0, units=units)


# ------------------------------------------------------------------------------ entry points ---
def check_file(path, *, source_format: str | None = None) -> ValidityReport:
    """The verdict on one uploaded file, whatever its format. Never raises for a bad file: a file
    that cannot be read is a reason ("unreadable"), not an exception."""
    from meshpipeline.cad.ingest import IngestError
    from meshpipeline.cad.ingest.canonical import canonicalise, resolve_format
    from meshpipeline.contracts.intake_formats import GeometryKind, format_for_key

    t0 = time.time()
    p = Path(path)
    try:
        key = source_format or resolve_format(p)
    except IngestError as exc:
        return _unreadable(str(exc), t0)
    fmt = format_for_key(key)
    if key == "ecxml":
        return unchecked("an ECXML thermal model states its parts and is read by its own builder; "
                         "the clean check judges CAD and triangle files", t0)
    if fmt is not None and fmt.kind is GeometryKind.surface:
        return check_surface_file(p, key)
    with tempfile.TemporaryDirectory(prefix="clean_check_") as tmp:
        try:
            canonical = canonicalise(p, Path(tmp), stem="geometry")
        except IngestError as exc:
            return _unreadable(str(exc), t0)
        except Exception as exc:  # noqa: BLE001 - a reader crash is a fact about the file
            return _unreadable(f"{type(exc).__name__}: {exc}", t0)
        if canonical.kind is GeometryKind.surface:      # a STEP made of triangles
            return check_surface_file(canonical.path, "stl")
        return check_cad_file(canonical.path)


def check_canonical(uploaded, canonical) -> ValidityReport:
    """The verdict for the geometry check: the uploaded bytes judged as the user sent them.

    A triangle file is judged on the triangles in the upload, BEFORE the ingest's safe cleanup
    (which drops doubled triangles and turns flipped ones): what was sent is what is certified.
    Exact CAD is judged on its canonical STEP (the bytes themselves, or the ingest's conversion of
    another CAD format). A STEP made of triangles is judged as triangles."""
    from meshpipeline.contracts.intake_formats import GeometryKind, format_for_key

    if canonical.source_format == "ecxml":
        return unchecked("an ECXML thermal model states its parts and is read by its own builder; "
                         "the clean check judges CAD and triangle files", time.time())
    fmt = format_for_key(canonical.source_format)
    if canonical.kind is GeometryKind.surface:
        if fmt is not None and fmt.kind is GeometryKind.surface:
            return check_surface_file(Path(uploaded), canonical.source_format)
        return check_surface_file(Path(canonical.path), "stl")
    return check_cad_file(Path(canonical.path))


def check_cad_file(path) -> ValidityReport:
    from meshpipeline.cad.validity_brep import check_shape

    t0 = time.time()
    try:
        from meshpipeline.cad.scout import read_cad

        shape = read_cad(Path(path))
        if shape is None or shape.IsNull():
            return _unreadable("OpenCASCADE read no shape from the file", t0)
    except Exception as exc:  # noqa: BLE001 - an unreadable file is a reason, not a crash
        return _unreadable(f"OpenCASCADE could not read the file ({type(exc).__name__})", t0)
    return check_shape(shape, t0=t0)


def check_surface_file(path, key: str = "stl") -> ValidityReport:
    from meshpipeline.cad.ingest.readers import read_surface

    t0 = time.time()
    try:
        mesh = read_surface(Path(path), key)
    except Exception as exc:  # noqa: BLE001 - an unreadable file is a reason, not a crash
        return _unreadable(f"the {key.upper()} file could not be read ({type(exc).__name__}: "
                           f"{str(exc)[:160]})", t0)
    return check_triangles(mesh.points, mesh.triangles, float32=key == "stl", t0=t0,
                           units="file units")


def unchecked(why: str, t0: float) -> ValidityReport:
    return ValidityReport(clean=False, kind="unchecked", findings=(Finding("unchecked", INFO, why),),
                          measured={}, complete=False, seconds=time.time() - t0)


def _unreadable(why: str, t0: float) -> ValidityReport:
    return verdict("unreadable", [Finding("unreadable", ERROR, f"the file could not be read: {why}")],
                   {}, complete=True, t0=t0, units="")


# --------------------------------------------------------------------------- triangle rules ---
def check_triangles(points, triangles, *, float32: bool = False, t0: float | None = None,
                    units: str = "file units", crossings: bool = True) -> ValidityReport:
    """The contract on a triangle surface. `points` (n, 3), `triangles` (m, 3) indices; corners
    at the same coordinates are one vertex however the file wrote them."""
    t0 = time.time() if t0 is None else t0
    P = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    T = np.asarray(triangles, dtype=np.int64).reshape(-1, 3)
    findings: list[Finding] = []
    measured: dict = {"triangles": int(len(T))}
    if len(T) == 0 or len(P) == 0:
        return verdict("triangles", [Finding("empty", ERROR, "the file holds no surface triangles")],
                       measured, complete=True, t0=t0, units=units)
    if len(T) > MAX_VERIFIED_TRIANGLES:
        return verdict("triangles", [Finding("not_verified", ERROR,
                                             f"the file has {len(T):,} triangles; the check verifies up "
                                             f"to {MAX_VERIFIED_TRIANGLES:,}, so it was not checked")],
                       measured, complete=False, t0=t0, units=units)
    if T.min() < 0 or T.max() >= len(P):
        return verdict("triangles", [Finding("unreadable", ERROR,
                                             "faces point at vertices that do not exist")],
                       measured, complete=True, t0=t0, units=units)
    used = P[np.unique(T)]
    bad = ~np.isfinite(used).all(axis=1)
    if bad.any():
        return verdict("triangles", [Finding("non_finite", ERROR,
                                             f"{int(bad.sum())} vertices have coordinates that are "
                                             "not numbers (NaN or infinity)", int(bad.sum()))],
                       measured, complete=True, t0=t0, units=units)
    # weld: corners at exactly the same coordinates are one vertex
    pts, inv = np.unique(P[T].reshape(-1, 3), axis=0, return_inverse=True)
    T = np.asarray(inv, dtype=np.int64).reshape(-1, 3)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    ext = hi - lo
    diag = float(np.linalg.norm(ext))
    step, digits = precision_of(pts)
    measured.update(vertices=int(len(pts)), bbox_min=_r(lo), bbox_max=_r(hi), diag=_r1(diag),
                    number_precision=digits)
    findings.extend(_plausibility(lo, hi, diag, float32=float32 or digits == "32-bit"))
    if diag <= 0.0:
        return verdict("triangles", findings, measured, complete=True, t0=t0, units=units)

    C = pts[T]
    # degenerate: collapsed corners or collinear ones
    collapsed = (T[:, 0] == T[:, 1]) | (T[:, 1] == T[:, 2]) | (T[:, 0] == T[:, 2])
    cr = np.cross(C[:, 1] - C[:, 0], C[:, 2] - C[:, 0])
    a2 = np.linalg.norm(cr, axis=1)
    lmax = np.max(np.stack([np.linalg.norm(C[:, 1] - C[:, 0], axis=1),
                            np.linalg.norm(C[:, 2] - C[:, 1], axis=1),
                            np.linalg.norm(C[:, 0] - C[:, 2], axis=1)], axis=1), axis=1)
    degenerate = collapsed | (a2 <= DEGENERATE_RATIO * np.maximum(lmax, 1e-300) ** 2)
    if degenerate.any():
        n = int(degenerate.sum())
        findings.append(Finding("degenerate_faces", ERROR,
                                f"{n} triangle(s) have zero area (corners collapsed or in a line)",
                                n, _places(C[degenerate].mean(axis=1), diag)))
    good = ~collapsed
    Tg = T[good]
    # duplicates: the same three corners written twice
    key = np.sort(Tg, axis=1)
    _, first, cnt = np.unique(key, axis=0, return_index=True, return_counts=True)
    if (cnt > 1).any():
        n = int((cnt - 1)[cnt > 1].sum())
        findings.append(Finding("duplicate_faces", ERROR, f"{n} triangle(s) are written twice", n,
                                _places(C[good][first[cnt > 1]].mean(axis=1), diag)))
    topo, piece, inward = _topology(pts, Tg, diag, measured)
    findings.extend(topo)
    complete = True
    if crossings:
        if len(Tg) > MAX_VERIFIED_TRIANGLES:
            complete = False
            findings.append(Finding("not_verified", ERROR,
                                    f"the file has {len(Tg):,} triangles; the check verifies up to "
                                    f"{MAX_VERIFIED_TRIANGLES:,}, so whether the surface passes "
                                    "through itself was not checked", 1))
        else:
            complete = _crossing_findings(pts, Tg, piece, inward, diag, findings, measured,
                                          tolerance=PRECISION_STEPS * step)
    return verdict("triangles", findings, measured, complete=complete, t0=t0, units=units)


def _plausibility(lo, hi, diag: float, *, float32: bool) -> list[Finding]:
    out: list[Finding] = []
    ext = hi - lo
    if diag <= 0.0:
        return [Finding("implausible_coordinates", ERROR, "every vertex sits at one point")]
    flat = [ax for ax in range(3) if ext[ax] <= FLAT_RATIO * diag]
    if flat:
        out.append(Finding("flat", ERROR,
                           f"the geometry has no thickness along {'/'.join('xyz'[a] for a in flat)}: "
                           "a sheet, not a solid", len(flat)))
    offset = float(np.max(np.abs(np.r_[lo, hi])))
    limit = FLOAT32_OFFSET_RATIO if float32 else FLOAT64_OFFSET_RATIO
    if offset > limit * diag:
        out.append(Finding("implausible_coordinates", ERROR,
                           f"the part sits {offset / diag:,.0f} of its own sizes from the origin; "
                           f"the file's number precision cannot draw its detail there",
                           1, ({"at": _r((lo + hi) / 2)},),
                           {"offset_ratio": round(offset / diag, 1), "limit": limit}))
    return out


def _topology(pts: np.ndarray, T: np.ndarray, diag: float,
              measured: dict) -> tuple[list[Finding], np.ndarray, np.ndarray]:
    """Closed, manifold, oriented, solid - from the edges the triangles share. Also returns which
    closed piece (triangles joined across edges) each triangle belongs to (-1: an open or
    non-manifold piece, or one enclosing nothing) and whether each piece faces inward."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    out: list[Finding] = []
    m = len(T)
    if m == 0:
        return out, np.zeros(0, dtype=np.int64), np.zeros(0, dtype=bool)
    he = np.concatenate([T[:, [0, 1]], T[:, [1, 2]], T[:, [2, 0]]])          # half-edges
    face = np.tile(np.arange(m), 3)
    corner = np.concatenate([np.arange(m) * 3, np.arange(m) * 3 + 1, np.arange(m) * 3 + 2])
    ekey = np.sort(he, axis=1)
    _, einv, ecnt = np.unique(ekey, axis=0, return_inverse=True, return_counts=True)
    einv = np.asarray(einv).ravel()
    use = ecnt[einv]
    n_edges = len(ecnt)
    boundary = use == 1
    nonman = use > 2
    measured.update(edges=int(n_edges), open_edges=int((ecnt == 1).sum()),
                    non_manifold_edges=int((ecnt > 2).sum()))

    # pieces: triangles joined across edges
    order = np.argsort(einv, kind="stable")
    se, sf = einv[order], face[order]
    same = se[1:] == se[:-1]
    a, b = sf[:-1][same], sf[1:][same]
    g = coo_matrix((np.ones(len(a), dtype=np.int8), (a, b)), shape=(m, m))
    n_pieces, piece = connected_components(g, directed=False)
    measured["pieces"] = int(n_pieces)

    if (ecnt == 1).any():
        out.append(_open_finding(pts, ekey[np.flatnonzero(boundary)], diag, int((ecnt == 1).sum())))
    if (ecnt > 2).any():
        n = int((ecnt > 2).sum())
        mid = pts[ekey[nonman]].mean(axis=1)
        out.append(Finding("non_manifold_edges", ERROR,
                           f"{n} edge(s) are shared by more than two faces", n, _places(mid, diag)))

    # orientation across edges exactly two triangles share
    two = np.flatnonzero(use == 2)
    o2 = two[np.argsort(einv[two], kind="stable")]
    h1, h2 = o2[0::2], o2[1::2]
    same_way = np.all(he[h1] == he[h2], axis=1)
    if same_way.any():
        n = int(same_way.sum())
        mid = pts[he[h1[same_way]]].mean(axis=1)
        out.append(Finding("inconsistent_orientation", ERROR,
                           f"{n} edge(s) join faces that disagree which side is out (flipped faces)",
                           n, _places(mid, diag)))

    # corners where surfaces meet at a point: the triangle fans round a vertex do not connect
    # an edge walked opposite ways joins start(h1)-end(h2) and end(h1)-start(h2); the same way,
    # start-start and end-end
    j1 = np.where(same_way, corner[h2], _next_corner(corner[h2]))
    j2 = np.where(same_way, _next_corner(corner[h2]), corner[h2])
    rows = np.r_[corner[h1], _next_corner(corner[h1])]
    cols = np.r_[j1, j2]
    cg = coo_matrix((np.ones(len(rows), dtype=np.int8), (rows, cols)), shape=(3 * m, 3 * m))
    _, fan = connected_components(cg, directed=False)
    vert_of_corner = T.reshape(-1)
    on_bad_edge = np.zeros(len(pts), dtype=bool)
    on_bad_edge[ekey[nonman].reshape(-1)] = True
    pairs = np.unique(np.c_[vert_of_corner, fan], axis=0)
    fans = np.bincount(pairs[:, 0], minlength=len(pts))
    pinched = (fans > 1) & ~on_bad_edge
    if pinched.any():
        n = int(pinched.sum())
        out.append(Finding("non_manifold_vertices", ERROR,
                           f"{n} corner(s) join surfaces that touch only at a point", n,
                           _places(pts[pinched], diag)))

    # every closed piece encloses volume and faces outward (a cavity faces in, inside another)
    C = pts[T]
    sv = np.einsum("ij,ij->i", C[:, 0], np.cross(C[:, 1], C[:, 2])) / 6.0
    vol = np.bincount(piece, weights=sv, minlength=n_pieces)
    open_piece = np.zeros(n_pieces, dtype=bool)
    open_piece[piece[face[boundary | nonman]]] = True
    plo = np.full((n_pieces, 3), np.inf)
    phi = np.full((n_pieces, 3), -np.inf)
    np.minimum.at(plo, piece, C.min(axis=1))
    np.maximum.at(phi, piece, C.max(axis=1))
    box = np.prod(np.maximum(phi - plo, 0.0), axis=1)
    closed = ~open_piece
    measured.update(closed_pieces=int(closed.sum()), volume=_r1(float(vol[closed].sum())))
    zero = closed & (np.abs(vol) <= ZERO_VOLUME_RATIO * np.maximum(box, diag ** 3 * 1e-30))
    if zero.any():
        n = int(zero.sum())
        out.append(Finding("zero_volume", ERROR,
                           f"{n} closed piece(s) enclose no volume (a closed double-sided sheet)", n,
                           tuple({"at": _r((plo[k] + phi[k]) / 2), "size": _r1(float(np.linalg.norm(phi[k] - plo[k])))}
                                 for k in np.flatnonzero(zero)[:MAX_PLACES])))
    neg = np.flatnonzero(closed & ~zero & (vol < 0))
    pos = np.flatnonzero(closed & ~zero & (vol > 0))
    inside_out = []
    for k in neg.tolist():
        held = (np.all(plo[pos] <= plo[k], axis=1) & np.all(phi[pos] >= phi[k], axis=1)
                & (box[pos] > box[k])) if len(pos) else np.zeros(0, dtype=bool)
        if not held.any():
            inside_out.append(k)
    if inside_out:
        n = len(inside_out)
        out.append(Finding("inside_out", ERROR,
                           f"{n} closed piece(s) face inward (inside out) and sit inside no other piece",
                           n, tuple({"at": _r((plo[k] + phi[k]) / 2),
                                     "size": _r1(float(np.linalg.norm(phi[k] - plo[k])))}
                                    for k in inside_out[:MAX_PLACES])))
    body = np.where((closed & ~zero)[piece], piece, -1).astype(np.int64)
    return out, body, vol < 0


def _open_finding(pts: np.ndarray, bedges: np.ndarray, diag: float, n_open: int) -> Finding:
    """The holes: open edges chained into loops, the longest first, and whether they all lie on
    one plane at the edge of the part (a half model cut on its symmetry plane)."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    vid, vinv = np.unique(bedges.reshape(-1), return_inverse=True)
    vinv = vinv.reshape(-1, 2)
    g = coo_matrix((np.ones(len(vinv), dtype=np.int8), (vinv[:, 0], vinv[:, 1])),
                   shape=(len(vid), len(vid)))
    n_loops, lab = connected_components(g, directed=False)
    seg = np.linalg.norm(pts[bedges[:, 0]] - pts[bedges[:, 1]], axis=1)
    loop_of_edge = lab[vinv[:, 0]]
    length = np.bincount(loop_of_edge, weights=seg, minlength=n_loops)
    places = []
    for k in np.argsort(-length)[:MAX_PLACES].tolist():
        v = pts[vid[lab == k]]
        places.append({"at": _r(v.mean(axis=0)), "length": _r1(float(length[k])),
                       "size": _r1(float(np.linalg.norm(v.max(axis=0) - v.min(axis=0))))})
    details: dict = {"loops": int(n_loops), "open_edges": int(n_open)}
    bv = pts[vid]
    spread = bv.max(axis=0) - bv.min(axis=0)
    plane = int(np.argmin(spread))
    if spread[plane] <= 1e-6 * diag:
        details["on_one_plane"] = f"{'xyz'[plane]} = {float(bv[0, plane]):.6g}"
    biggest = places[0] if places else {}
    msg = (f"the surface is not closed: {n_open} open edge(s) form {n_loops} hole(s)"
           + (f"; the largest runs {biggest['length']:g} round, at {biggest['at']}" if biggest else ""))
    if "on_one_plane" in details:
        msg += f" - every open edge lies on the plane {details['on_one_plane']} (a half model?)"
    return Finding("open_boundary", ERROR, msg, int(n_loops), tuple(places), details)


def _crossing_findings(pts: np.ndarray, T: np.ndarray, piece: np.ndarray, inward: np.ndarray,
                       diag: float, findings: list[Finding], measured: dict, *,
                       tolerance: float) -> bool:
    """No crossing, and no closed piece inside another's material. `piece`: each triangle's
    closed piece, -1 for none (a crossing there is still a crossing, of the surface itself)."""
    from meshpipeline.cad.crossings import MAX_HITS, find_crossings, nested_bodies

    # faces that touch are only as coplanar as the file can write them
    found = find_crossings(pts, T, tolerance=tolerance)
    measured["crossing_pairs_tested"] = int(found.tested)
    overlaps: list = []
    if found.count:
        I, J = found.pairs[:, 0], found.pairs[:, 1]
        mid = (pts[T[I]].mean(axis=1) + pts[T[J]].mean(axis=1)) / 2
        between = (piece[I] != piece[J]) & (piece[I] >= 0) & (piece[J] >= 0)
        if (~between).any():
            n = int((~between).sum())
            findings.append(Finding("self_intersection", ERROR,
                                    f"the surface passes through itself: {n} crossing triangle "
                                    f"pair(s){'+' if found.count >= MAX_HITS else ''}", n,
                                    _places(mid[~between], diag)))
        if between.any():
            bp = np.unique(np.sort(np.c_[piece[I[between]], piece[J[between]]], axis=1), axis=0)
            overlaps.extend(_places(mid[between], diag)[:MAX_PLACES])
            seen = {(int(a), int(b)) for a, b in bp}
        else:
            seen = set()
    else:
        seen = set()
    # a body wholly inside another, or sharing a face while running into it, crosses nothing
    nest = nested_bodies(pts, T, piece, inward)
    extra = [(k, j, at) for k, j, at in nest.found if (min(k, j), max(k, j)) not in seen]
    overlaps.extend({"at": _r(at), "bodies": [int(k) + 1, int(j) + 1]} for k, j, at in extra[:MAX_PLACES])
    n_pairs = len(seen) + len(extra)
    if n_pairs:
        findings.append(Finding("bodies_overlap", ERROR,
                                f"{n_pairs} pair(s) of separate bodies pass into each other",
                                n_pairs, tuple(overlaps[:MAX_PLACES])))
    if not nest.complete:
        findings.append(Finding("not_verified", ERROR,
                                "there are too many bodies inside each other's boxes to finish "
                                "the overlap search, so whether bodies overlap was not checked"))
    if not found.complete:
        findings.append(Finding("not_verified", ERROR,
                                "the surface is too dense to finish the crossing search within its "
                                "budget, so whether it passes through itself was not checked"))
    return found.complete and nest.complete


# ------------------------------------------------------------------------------- helpers -------
def precision_of(pts: np.ndarray) -> tuple[float, str]:
    """The step between neighbouring numbers this file can write near its largest coordinate,
    read from the coordinates themselves: 32-bit numbers (an STL, most GLB and PLY), a fixed count
    of printed decimals (most OBJ, ASCII STL, DAE), or full 64-bit numbers."""
    big = float(np.max(np.abs(pts))) if pts.size else 0.0
    if big == 0.0:
        return 0.0, "64-bit"
    if np.array_equal(pts.astype(np.float32).astype(np.float64), pts):
        return big * 2.0 ** -23, "32-bit"
    for d in range(0, 13):
        if np.array_equal(np.round(pts, d), pts):
            return max(10.0 ** -d, big * 2.0 ** -52), f"{d} decimals"
    return big * 2.0 ** -52, "64-bit"


def _next_corner(c: np.ndarray) -> np.ndarray:
    """The corner a half-edge ends at: the next corner of the same triangle."""
    return (c // 3) * 3 + (c % 3 + 1) % 3


def _places(xyz: np.ndarray, diag: float) -> tuple[dict, ...]:
    """Points grouped into spots (cells of 1/20 of the part), the most crowded first."""
    xyz = np.asarray(xyz, dtype=np.float64).reshape(-1, 3)
    if not len(xyz):
        return ()
    cell = max(diag / 20.0, 1e-300)
    q = np.floor((xyz - xyz.min(axis=0)) / cell).astype(np.int64)
    _, lab, cnt = np.unique(q, axis=0, return_inverse=True, return_counts=True)
    lab = np.asarray(lab).ravel()
    out = []
    for k in np.argsort(-cnt, kind="stable")[:MAX_PLACES].tolist():
        pts = xyz[lab == k]
        out.append({"at": _r(pts.mean(axis=0)), "count": int(cnt[k]),
                    "size": _r1(float(np.linalg.norm(pts.max(axis=0) - pts.min(axis=0))))})
    return tuple(out)


def _r(v) -> list[float]:
    return [float(f"{float(x):.6g}") for x in v]


def _r1(x: float) -> float:
    return float(f"{float(x):.6g}")
