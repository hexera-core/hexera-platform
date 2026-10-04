# Responsibility: Turn a CAD solid into a triangulated surface.
# Boundaries: tessellation only; the unit and scale it works in are decided before it runs.
from __future__ import annotations

import logging
import math
from pathlib import Path

import numpy as np

from meshpipeline.cad.stl_io import read_stl_triangles

logger = logging.getLogger(__name__)


def is_iges(path) -> bool:
    """Whether a CAD file IS IGES, read from its content, not its name. Staging copies every CAD
    upload to geometry.step for the engines that read the B-rep, so an IGES upload arrived under a
    .step name and the STEP reader refused it ("OpenCASCADE could not read CAD file:
    geometry.step": cfMesh internal on every IGES of the lab test set, 2026-10-04). A STEP file
    opens with ISO-10303-21; an IGES file is 80-column records whose 73rd column names the
    section (S, G, D, P, T). The name decides only when the content says neither."""
    p = Path(path)
    try:
        with open(p, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        head = b""
    text = head.lstrip()
    if text.startswith(b"ISO-10303-21"):
        return False
    first = head.splitlines()[0] if head else b""
    if len(first) >= 73 and first[72:73] in (b"S", b"G") and b"ISO-10303" not in head:
        return True
    return p.suffix.lower() in (".igs", ".iges")


def _occ_to_metres(prepared):
    from meshpipeline.cad.normalise import occ_scale_transform

    if prepared is None:
        raise ValueError(
            "CAD tessellation needs the OCC-output coordinate state; without it the conversion "
            "to metres would be a guess that is right only for well-formed files")
    return occ_scale_transform(prepared)


def tessellate_to_stl(geom_path, out_stl, *, prepared=None, angular_deflection: float = 0.2,
                      linear_deflection: float | None = None) -> Path:
    import shutil as _sh
    geom_path, out_stl = Path(geom_path), Path(out_stl)
    if geom_path.suffix.lower() == ".stl":
        if geom_path.resolve() != out_stl.resolve():
            _sh.copy2(geom_path, out_stl)
        return out_stl
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.StlAPI import StlAPI_Writer
    if is_iges(geom_path):
        from OCP.IGESControl import IGESControl_Reader as _Reader
    else:
        from OCP.STEPControl import STEPControl_Reader as _Reader
    reader = _Reader()
    if reader.ReadFile(str(geom_path)) != IFSelect_RetDone:
        raise RuntimeError(f"OpenCASCADE could not read CAD file: {geom_path.name}")
    reader.TransferRoots()
    shape = reader.OneShape()
    trsf = _occ_to_metres(prepared)
    shape = BRepBuilderAPI_Transform(shape, trsf, True).Shape()
    box = Bnd_Box(); BRepBndLib.Add_s(shape, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    diag = ((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2) ** 0.5
    lin = linear_deflection if linear_deflection is not None else diag / 2500.0
    BRepMesh_IncrementalMesh(shape, lin, False, angular_deflection, True)
    StlAPI_Writer().Write(shape, str(out_stl))
    return out_stl


# The declaration-vs-face tolerance of opening selection: the same symmetric 25% area band
# the engine binder uses, applied to BOTH size measures a candidate offers (its own face
# area, and - for an annular ring face - the area its inner wire encloses).
_AREA_BAND_LO = 0.75
_AREA_BAND_HI = 1.25
# Per-dimension band for shape agreement (declared bore diameter / W x H against a ring's
# inner-wire extents): the linear equivalent of the area band.
_LIN_BAND_LO = _AREA_BAND_LO ** 0.5
_LIN_BAND_HI = _AREA_BAND_HI ** 0.5
# Candidates whose relative size disagreement lands within the same 1% are EQUALLY matching -
# the difference is rim-discretization/manufacturing noise, not evidence - and among equals
# the SMALLEST face wins: the thin end ring hugging the bore, never the broad flange annulus
# around the very same hole. 1% sits an order above rim-discretization drift (~0.1% at the
# default deflection) and well below the 25% band.
_SIZE_ERR_QUANTUM = 0.01
# Centroid distances within a micrometre are ONE location: concentric ring faces (a duct
# wall's end ring inside its stacked flange annuli) differ there only by float noise.
_DIST_QUANTUM_M = 1e-6


def _band_ok(measured: float, declared: float) -> bool:
    ratio = measured / declared
    return _AREA_BAND_LO <= ratio <= _AREA_BAND_HI


# How much of its own W x H box an opening fills: pi/4 for a circle, 1 for a rectangle.
# The fill factors sit 0.215 apart, so +/-0.1 tells the shapes apart while forgiving
# rounded rectangle corners and chamfered bores.
_FILL_CIRCLE = math.pi / 4.0
_FILL_RECT = 1.0
_FILL_TOL = 0.1


def _shape_agrees(opening: dict, port: dict) -> bool:
    """Dimension-level agreement between a declared shape and a ring candidate's inner-wire
    opening: circles by bore diameter, rectangles by W x H (order-free), each backed by the
    opening's fill factor - an equal-area square is only 11% narrower than the circle (the
    linear band alone cannot reject it) but fills its box 27% fuller. A port declaring only
    an area has no shape to check; an opening without measured extents cannot disagree."""
    wh = opening.get("wh_m")
    if not wh or not (wh[0] > 0.0 and wh[1] > 0.0):
        return True
    if port.get("d_m"):
        want = (float(port["d_m"]), float(port["d_m"]))
        fill_want = _FILL_CIRCLE
    elif port.get("wh_m"):
        want = (float(port["wh_m"][0]), float(port["wh_m"][1]))
        fill_want = _FILL_RECT
    else:
        return True
    (a, b), (p, q) = sorted(wh), sorted(want)
    if not (_LIN_BAND_LO <= a / p <= _LIN_BAND_HI
            and _LIN_BAND_LO <= b / q <= _LIN_BAND_HI):
        return False
    fill = float(opening.get("area_m2") or 0.0) / (wh[0] * wh[1])
    return abs(fill - fill_want) <= _FILL_TOL


def select_declared_openings(candidates: list, declared: list) -> list:
    """Pick which candidate faces are the DECLARED openings. candidates: (idx, area_m2,
    centroid[, opening]) per planar face, where the optional opening describes what an
    ANNULAR (ring) face's largest inner wire encloses: {area_m2, centroid, wh_m}.
    declared: {name, area_m2|None, near_m|None[, d_m, wh_m]} per port.

    A declared size may match a candidate by EITHER measure, inside the same +/-25% band
    (the binder's own tolerance): the face's own area (a solid-model disc IS its opening),
    or the area a ring face's inner wire encloses - a thin-walled duct's end ring is a few
    thousand mm² of metal around a half-metre bore, and the declaration talks about the
    bore. An inner-wire match must also agree in shape (bore diameter for circles, W x H
    for rectangles) when the declaration states one.

    Hints claim the nearest unclaimed face; among faces at one location (a concentric ring
    stack: the duct end ring inside its flange annuli) the best size agreement wins, and
    within noise-equal agreement the smallest face - the ring hugging the opening, never
    the flange annulus around it. Deterministic: ports in name order, hinted ports first.
    A port no face can satisfy refuses with the measured face list - the geometry and the
    words disagree, and only the user can settle that."""
    remaining: dict = {}
    for cand in candidates:
        opening = cand[3] if len(cand) > 3 else None
        remaining[int(cand[0])] = (float(cand[1]), tuple(cand[2]), opening)
    chosen: list[int] = []

    def _dist(p, q):
        return sum((p[k] - q[k]) ** 2 for k in range(3)) ** 0.5

    # WHAT FILLS AN OPENING. A ring face's inner wire encloses the bore; a body standing in the
    # bore (the centre rod of an annular passage, a second solid of the file) shows its own flat
    # end face inside that wire, in the same plane. The flow crosses only what is left: the bore
    # less those faces. annular_001 declares its 151 mm bore around a 125 mm rod, 5730 mm2 of
    # annulus; the ring's inner wire encloses 17923 mm2 and the rod's end disc 12223 mm2, and with
    # neither measure inside the band, every engine refused the part before meshing (2026-10-04).
    def _filled(own_idx, opening) -> float:
        if opening is None or not opening.get("area_m2"):
            return 0.0
        wh = opening.get("wh_m") or ()
        reach = 0.5 * min(wh) if len(wh) == 2 and min(wh) > 0 else (
            float(opening["area_m2"]) / math.pi) ** 0.5
        oc = tuple(opening.get("centroid") or ())
        if len(oc) != 3:
            return 0.0
        return sum(a for i, (a, c, _o) in all_faces.items()
                   if i != own_idx and a < float(opening["area_m2"]) and _dist(c, oc) < reach)

    all_faces = dict(remaining)
    filled = {i: _filled(i, o) for i, (_a, _c, o) in remaining.items()}

    def _size_err(idx, area, opening, port):
        # the smallest relative disagreement any admissible measure achieves inside the
        # band, or None when the declared size fits by no measure
        declared_area = float(port["area_m2"])
        errs = []
        if _band_ok(area, declared_area):
            errs.append(abs(area / declared_area - 1.0))
        if (opening is not None and opening.get("area_m2")
                and _shape_agrees(opening, port)):
            for measure in {float(opening["area_m2"]),
                            float(opening["area_m2"]) - filled.get(idx, 0.0)}:
                if measure > 0.0 and _band_ok(measure, declared_area):
                    errs.append(abs(measure / declared_area - 1.0))
        return min(errs) if errs else None

    hinted = sorted((p for p in declared if p.get("near_m")), key=lambda p: str(p.get("name")))
    sized = sorted((p for p in declared if not p.get("near_m")),
                   key=lambda p: str(p.get("name")))
    for port in hinted + sized:
        best = None
        for idx, (area, cen, opening) in remaining.items():
            if port.get("area_m2"):
                err = _size_err(idx, area, opening, port)
                if err is None:
                    continue
                err_bucket = int(err / _SIZE_ERR_QUANTUM)
                tie_area = area
            else:
                err_bucket, tie_area = 0, 0.0   # no declared size: hint alone decides
            if port.get("near_m"):
                d = _dist(tuple(port["near_m"]), cen)
                score = (round(d / _DIST_QUANTUM_M), err_bucket, tie_area, idx)
            else:
                score = (0, err_bucket, tie_area, idx)
            if best is None or score < best[0]:
                best = (score, idx)
        if best is None:
            rows = []
            for i, (a, c, opening) in sorted(remaining.items()):
                row = (f"face[{i}]: {a * 1e6:.0f} mm² at ({c[0]:.3f}, {c[1]:.3f}, "
                       f"{c[2]:.3f}) m")
                if opening is not None and opening.get("area_m2"):
                    w, h = opening.get("wh_m") or (0.0, 0.0)
                    row += (f" (ring face; inner opening "
                            f"{float(opening['area_m2']) * 1e6:.0f} mm²"
                            + (f", {w * 1e3:.0f} x {h * 1e3:.0f} mm" if w and h else "")
                            + ")")
                rows.append(row)
            raise ValueError(
                f"declared port {port.get('name')!r} matches none of the remaining flat "
                f"faces - measured: {'; '.join(rows)}. State the port's size or rough "
                "location so the opening can be identified, or correct the declaration.")
        chosen.append(best[1])
        del remaining[best[1]]
    return chosen


def _tri_area(tri: tuple) -> float:
    a, b, c = tri
    ux, uy, uz = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    vx, vy, vz = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    cx, cy, cz = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
    return 0.5 * math.sqrt(cx * cx + cy * cy + cz * cz)


def _newell_frame(pts: list) -> tuple | None:
    """Best-fit plane of a closed 3D polyline: ((unit normal), (centroid)), or None when
    the polyline is degenerate (no measurable enclosed area in any projection)."""
    nx = ny = nz = 0.0
    m = len(pts)
    for i in range(m):
        px, py, pz = pts[i]
        qx, qy, qz = pts[(i + 1) % m]
        nx += (py - qy) * (pz + qz)
        ny += (pz - qz) * (px + qx)
        nz += (px - qx) * (py + qy)
    norm = math.sqrt(nx * nx + ny * ny + nz * nz)
    if norm <= 0.0:
        return None
    c = tuple(sum(p[k] for p in pts) / m for k in range(3))
    return (nx / norm, ny / norm, nz / norm), c


def _convex_hull_2d(pts: list) -> list:
    """Convex hull of 2D points (Andrew monotone chain), counter-clockwise, no
    duplicates. Degenerate inputs (all collinear) return the chain itself."""
    pts = sorted(set(pts))
    if len(pts) < 3:
        return pts

    def _half(seq):
        out: list = []
        for p in seq:
            while len(out) > 1 and ((out[-1][0] - out[-2][0]) * (p[1] - out[-2][1])
                                    - (out[-1][1] - out[-2][1]) * (p[0] - out[-2][0])) <= 0:
                out.pop()
            out.append(p)
        return out[:-1]

    return _half(pts) + _half(list(reversed(pts)))


def _min_rect_wh(p2: list) -> tuple:
    """(short, long) side lengths of the minimum-area rectangle enclosing 2D points
    (convex hull + rotating calipers). Rotation-independent, which is what a declared
    opening needs: a rectangle measures its true sides at any in-plane orientation and a
    circle measures d x d, where axis-aligned extents would inflate a rotated rectangle."""
    hull = _convex_hull_2d([tuple(p) for p in p2])
    if len(hull) < 3:
        xs = [p[0] for p in p2] or [0.0]
        ys = [p[1] for p in p2] or [0.0]
        return tuple(sorted((max(xs) - min(xs), max(ys) - min(ys))))
    best: tuple | None = None
    for i in range(len(hull)):
        ex = hull[(i + 1) % len(hull)][0] - hull[i][0]
        ey = hull[(i + 1) % len(hull)][1] - hull[i][1]
        el = math.hypot(ex, ey)
        if el <= 0.0:
            continue
        ux, uy = ex / el, ey / el
        us = [p[0] * ux + p[1] * uy for p in hull]
        vs = [-p[0] * uy + p[1] * ux for p in hull]
        w, h = max(us) - min(us), max(vs) - min(vs)
        if best is None or w * h < best[0] * best[1]:
            best = (w, h)
    return tuple(sorted(best)) if best else (0.0, 0.0)


def _rim_measure(pts: list) -> dict | None:
    """What a closed planar rim polyline encloses: {"area_m2", "centroid", "wh_m"}, or
    None when degenerate. The area is half the Newell normal's magnitude - exact for a
    planar polygon - and wh_m are the minimum enclosing rectangle's sides in the rim's
    own plane, shortest first."""
    nx = ny = nz = 0.0
    m = len(pts)
    if m < 3:
        return None
    for i in range(m):
        px, py, pz = pts[i]
        qx, qy, qz = pts[(i + 1) % m]
        nx += (py - qy) * (pz + qz)
        ny += (pz - qz) * (px + qx)
        nz += (px - qx) * (py + qy)
    norm = math.sqrt(nx * nx + ny * ny + nz * nz)
    if norm <= 0.0:
        return None
    n = (nx / norm, ny / norm, nz / norm)
    c = tuple(sum(p[k] for p in pts) / m for k in range(3))
    ax = (1.0, 0.0, 0.0) if abs(n[0]) < 0.9 else (0.0, 1.0, 0.0)
    ux, uy, uz = (n[1] * ax[2] - n[2] * ax[1], n[2] * ax[0] - n[0] * ax[2],
                  n[0] * ax[1] - n[1] * ax[0])
    un = math.sqrt(ux * ux + uy * uy + uz * uz)
    ux, uy, uz = ux / un, uy / un, uz / un
    vx, vy, vz = (n[1] * uz - n[2] * uy, n[2] * ux - n[0] * uz, n[0] * uy - n[1] * ux)
    p2 = [((p[0] - c[0]) * ux + (p[1] - c[1]) * uy + (p[2] - c[2]) * uz,
           (p[0] - c[0]) * vx + (p[1] - c[1]) * vy + (p[2] - c[2]) * vz) for p in pts]
    return {"area_m2": 0.5 * norm, "centroid": c, "wh_m": _min_rect_wh(p2)}


def _ring_membrane(ring: list) -> list:
    """Membrane triangles spanning one closed rim ring.

    Ear clipping in the ring's best-fit plane, so a long thin rim (a sliver face's
    boundary) is filled by triangles that hug the rim instead of a centroid fan whose
    chords cut far off the surface; whatever a self-crossing projection leaves over is
    closed with a centroid fan - topologically sealed even where the membrane is not
    pretty. Rim VERTICES are reused verbatim, so the membrane's rim edges coincide
    exactly with the surrounding surface's triangle edges (no new cracks)."""
    m = len(ring)
    if m < 3:
        return []
    frame = _newell_frame(ring)
    if frame is None:
        return []
    (nx, ny, nz), c = frame
    ax = (1.0, 0.0, 0.0) if abs(nx) < 0.9 else (0.0, 1.0, 0.0)
    ux, uy, uz = (ny * ax[2] - nz * ax[1], nz * ax[0] - nx * ax[2], nx * ax[1] - ny * ax[0])
    un = math.sqrt(ux * ux + uy * uy + uz * uz)
    ux, uy, uz = ux / un, uy / un, uz / un
    vx, vy, vz = (ny * uz - nz * uy, nz * ux - nx * uz, nx * uy - ny * ux)
    p2 = [((p[0] - c[0]) * ux + (p[1] - c[1]) * uy + (p[2] - c[2]) * uz,
           (p[0] - c[0]) * vx + (p[1] - c[1]) * vy + (p[2] - c[2]) * vz) for p in ring]
    signed2 = sum(p2[i][0] * p2[(i + 1) % m][1] - p2[(i + 1) % m][0] * p2[i][1]
                  for i in range(m))
    idx = list(range(m))
    if signed2 < 0:
        idx.reverse()
    scale = max((abs(x) for xy in p2 for x in xy), default=0.0)
    eps2 = 1e-12 * (scale * scale if scale > 0 else 1.0)

    def _cross(i: int, j: int, k: int) -> float:
        (x1, y1), (x2, y2), (x3, y3) = p2[i], p2[j], p2[k]
        return (x2 - x1) * (y3 - y1) - (x3 - x1) * (y2 - y1)

    def _contains(i: int, j: int, k: int, w: int) -> bool:
        return (_cross(i, j, w) >= -eps2 and _cross(j, k, w) >= -eps2
                and _cross(k, i, w) >= -eps2)

    tris: list = []
    while len(idx) > 3:
        n_now = len(idx)
        best: tuple | None = None       # (3D diagonal length, position) - the SHORTEST
        for s in range(n_now):          # diagonal keeps the membrane hugging the rim
            i, j, k = idx[s - 1], idx[s], idx[(s + 1) % n_now]
            if _cross(i, j, k) < -eps2:
                continue                                    # reflex corner, not an ear
            if any(_contains(i, j, k, w) for w in idx if w not in (i, j, k)):
                continue
            diag3 = math.dist(ring[i], ring[k])
            if best is None or diag3 < best[0]:
                best = (diag3, s)
        if best is None:
            break
        s = best[1]
        i, j, k = idx[s - 1], idx[s], idx[(s + 1) % len(idx)]
        tris.append((ring[i], ring[j], ring[k]))
        idx.pop(s)
    if len(idx) == 3:
        tris.append((ring[idx[0]], ring[idx[1]], ring[idx[2]]))
    elif len(idx) > 3:
        cc = tuple(sum(ring[i][k] for i in idx) / len(idx) for k in range(3))
        for s in range(len(idx)):
            tris.append((cc, ring[idx[s]], ring[idx[(s + 1) % len(idx)]]))
    return [t for t in tris if t[0] != t[1] and t[1] != t[2] and t[0] != t[2]]


def _open_rim_rings(tris: list) -> tuple[list, list]:
    """(closed rings, tangled components) of the boundary edges - edges used by exactly
    one triangle - of a triangle soup. Vertices compare exactly: the right equality for
    triangles that came off one shared tessellation, and for STL float32 round-trips of
    it. A component that is an open CHAIN with exactly two loose ends is returned as a
    ring too, closed across the tiny gap between the ends (neighbouring faces can
    discretize a sharp sliver tip a few microns apart); a component with any other
    degree structure cannot be walked as a ring and is returned as tangled."""
    use: dict = {}
    for a, b, c in tris:
        ta, tb, tc = tuple(a), tuple(b), tuple(c)
        for p, q in ((ta, tb), (tb, tc), (tc, ta)):
            if p == q:
                continue
            key = frozenset((p, q))
            use[key] = use.get(key, 0) + 1
    adj: dict = {}
    for e, n in use.items():
        if n != 1:
            continue
        p, q = tuple(e)
        adj.setdefault(p, []).append(q)
        adj.setdefault(q, []).append(p)
    rings: list = []
    tangled: list = []
    seen: set = set()
    for start in adj:
        if start in seen:
            continue
        comp = {start}
        queue = [start]
        while queue:
            v = queue.pop()
            for w in adj[v]:
                if w not in comp:
                    comp.add(w)
                    queue.append(w)
        seen |= comp
        loose = [v for v in comp if len(adj[v]) == 1]
        if any(len(adj[v]) > 2 for v in comp) or len(loose) not in (0, 2):
            tangled.append(sorted(comp))
            continue
        first = loose[0] if loose else start
        ring = [first, adj[first][0]]
        while True:
            nbs = adj[ring[-1]]
            nxt = [w for w in nbs if w != ring[-2]]
            if not nxt:
                break                       # the other loose end - the chain is done
            if nxt[0] == first:
                break                       # closed back to the start
            ring.append(nxt[0])
        rings.append(ring)
    return rings, tangled


def seal_open_rims(tris: list) -> tuple[list, list[dict]]:
    """Membrane triangles closing every open rim ring of a triangle soup, plus a per-rim
    report. An internal-flow staged surface must be CLOSED - the carve floods from a
    point inside the fluid and keeps everything it can reach, so any gap (a face OCC
    skipped with null triangulation, an open shell) hands it the exterior void. Rings
    are sealed unconditionally: a boundary edge in the staged soup is never legitimate
    here. A tangled boundary component (vertices without exactly two boundary
    neighbours) cannot be sealed and is reported with sealed=False."""
    rings, tangled = _open_rim_rings(tris)
    membranes: list = []
    report: list[dict] = []
    for ring in rings:
        tri_m = _ring_membrane(ring)
        membranes.extend(tri_m)
        length = sum(math.dist(ring[i], ring[(i + 1) % len(ring)])
                     for i in range(len(ring)))
        c = [round(sum(p[k] for p in ring) / len(ring), 6) for k in range(3)]
        report.append({"edges": len(ring), "length": round(length, 6), "centroid": c,
                       "sealed": bool(tri_m)})
    for comp in tangled:
        c = [round(sum(p[k] for p in comp) / len(comp), 6) for k in range(3)]
        report.append({"edges": len(comp), "length": None, "centroid": c,
                       "sealed": False})
    return membranes, report


# #
# THE CARVE SURFACE, ASKED ALONG RAYS. snappyHexMesh keeps the region of space that holds
# locationInMesh and is bounded by the staged surface - wall, port caps, every seal. For a
# hollow wall the fluid is a region that surface CLOSES: from inside it every ray stops on the
# surface, while from the exterior void some ray reaches open space. "Not in the metal" cannot
# tell the two apart - bend_elbow_021 was seeded 0.5 mm outside its own inlet cap, not in the
# metal, and the carve kept the whole exterior (8579 faces on the blockMesh 'outer' patch).
# #
def _sphere_directions(n: int) -> list:
    # evenly spread unit vectors (a Fibonacci lattice), turned off the lattice's own zero
    # angle so no ray runs exactly in an axis plane, along an axis-aligned face
    golden = math.pi * (3.0 - math.sqrt(5.0))
    out = []
    for i in range(n):
        z = 1.0 - (2.0 * i + 1.0) / n
        r = math.sqrt(max(0.0, 1.0 - z * z))
        out.append((r * math.cos(golden * i + 0.5), r * math.sin(golden * i + 0.5), z))
    return out


_ENCLOSURE_RAYS = _sphere_directions(64)


class CarveRays:
    """A triangle soup, vectorised for ray casts (Moller-Trumbore over every triangle)."""

    def __init__(self, tris: list) -> None:
        t = np.asarray(tris, dtype=float).reshape(-1, 3, 3)
        self._v0 = t[:, 0]
        self._e1 = t[:, 1] - t[:, 0]
        self._e2 = t[:, 2] - t[:, 0]
        self._scale = np.linalg.norm(np.cross(self._e1, self._e2), axis=1)

    def first_hit(self, origin, direction, t_min: float = 0.0) -> float:
        """Distance along the unit `direction` to the nearest crossing beyond t_min; inf when
        the ray crosses nothing."""
        d = np.asarray(direction, dtype=float)
        s = np.asarray(origin, dtype=float) - self._v0
        p = np.cross(d, self._e2)
        det = np.einsum("ij,ij->i", self._e1, p)
        live = np.abs(det) > 1e-12 * self._scale        # a ray in a triangle's plane misses it
        inv = np.divide(1.0, det, out=np.zeros_like(det), where=live)
        u = np.einsum("ij,ij->i", s, p) * inv
        q = np.cross(s, self._e1)
        v = (q @ d) * inv
        t = np.einsum("ij,ij->i", self._e2, q) * inv
        eps = 1e-9                                       # an edge hit counts on both triangles
        hit = live & (u >= -eps) & (v >= -eps) & (u + v <= 1.0 + eps) & (t > t_min)
        return float(t[hit].min()) if hit.any() else math.inf

    def enclosed_clearance(self, point) -> float | None:
        """The distance to the nearest stop when EVERY ray from `point` stops on the surface;
        None as soon as one reaches open space - the point is in the exterior void."""
        nearest = math.inf
        for d in _ENCLOSURE_RAYS:
            t = self.first_hit(point, d)
            if t == math.inf:
                return None
            nearest = min(nearest, t)
        return nearest

    def clear_between(self, a, b) -> bool:
        """The straight segment from `a` (on the surface - a port's cap) to `b` crosses
        nothing: `b` is in the region `a`'s side of the surface opens into."""
        seg = [b[k] - a[k] for k in range(3)]
        length = math.sqrt(sum(v * v for v in seg))
        if length <= 0.0:
            return False
        d = [v / length for v in seg]
        return self.first_hit(a, d, t_min=1e-4 * length) >= length


def tessellate_internal(geom_path, out_dir, *, prepared=None, angular_deflection: float = 0.2,
                        linear_deflection: float | None = None,
                        opening_faces: list[int] | None = None,
                        declared_ports: list | None = None,
                        fluid_solid: bool | None = None) -> dict:
    import math as _m

    from OCP.BRep import BRep_Builder, BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_Transform
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepIntCurveSurface import BRepIntCurveSurface_Inter
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRepTools import BRepTools, BRepTools_WireExplorer
    from OCP.GeomAbs import GeomAbs_Plane
    from OCP.gp import gp_Dir, gp_Lin, gp_Pnt
    from OCP.GProp import GProp_GProps
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.StlAPI import StlAPI_Writer
    from OCP.TopAbs import (
        TopAbs_FACE,
        TopAbs_IN,
        TopAbs_REVERSED,
        TopAbs_SOLID,
        TopAbs_WIRE,
    )
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS, TopoDS_Compound

    from meshpipeline.cad.stl_io import write_stl_binary

    geom_path = Path(geom_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if is_iges(geom_path):
        from OCP.IGESControl import IGESControl_Reader as _Reader
    else:
        from OCP.STEPControl import STEPControl_Reader as _Reader
    reader = _Reader()
    if reader.ReadFile(str(geom_path)) != IFSelect_RetDone:
        raise RuntimeError(f"OpenCASCADE could not read CAD file: {geom_path.name}")
    reader.TransferRoots()
    shape = reader.OneShape()
    trsf = _occ_to_metres(prepared)
    shape = BRepBuilderAPI_Transform(shape, trsf, True).Shape()

    # EVERY SOLID OF THE FILE. A file can hold several: an annular passage's pipe and the centre
    # rod modelled as a body of its own (all ten annular corpus parts), an insert, a baffle, a
    # tube bundle. The fluid is the space they bound together, so "in the material" means in ANY
    # of them - the first alone saw the rod and called the pipe's own metal open space.
    solids: list = []
    solid_exp = TopExp_Explorer(shape, TopAbs_SOLID)
    while solid_exp.More():
        solids.append(TopoDS.Solid_s(solid_exp.Current()))
        solid_exp.Next()
    if not solids:
        raise RuntimeError(
            "internal-flow input is not a watertight SOLID - the fluid volume must be a "
            "closed solid (a loose surface/shell is the pipe skin, not the flow passage)")
    classifiers = [BRepClass3d_SolidClassifier(s) for s in solids]

    def _inside(p) -> bool:
        for cls in classifiers:
            cls.Perform(gp_Pnt(*p), 1e-9)
            if cls.State() == TopAbs_IN:
                return True
        return False

    diag = 0.0
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    box = Bnd_Box(); BRepBndLib.Add_s(shape, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    diag = ((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2) ** 0.5
    lin = linear_deflection if linear_deflection is not None else diag / 2500.0
    BRepMesh_IncrementalMesh(shape, lin, False, angular_deflection, True)

    # classify faces: planar -> opening candidate, curved -> wall (see docstring)
    faces: list = []
    e = TopExp_Explorer(shape, TopAbs_FACE)
    while e.More():
        faces.append(TopoDS.Face_s(e.Current())); e.Next()

    def _face_props(f):
        g = GProp_GProps(); BRepGProp.SurfaceProperties_s(f, g)
        c = g.CentreOfMass()
        return g.Mass(), (c.X(), c.Y(), c.Z())

    def _wires_of(f):
        wires = []
        we = TopExp_Explorer(f, TopAbs_WIRE)
        while we.More():
            wires.append(TopoDS.Wire_s(we.Current())); we.Next()
        return wires

    def _rim_polyline(f, wire):
        # the wire's discretization inside f's own triangulation, so any membrane built
        # on these points is vertex-identical with the surrounding surface triangles
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(f, loc)
        if tri is None:
            return []
        trsf = loc.Transformation()
        pts: list = []
        wexp = BRepTools_WireExplorer(wire, f)
        while wexp.More():
            edge = wexp.Current()
            pol = BRep_Tool.PolygonOnTriangulation_s(edge, tri, loc)
            if pol is None:
                return []
            nodes = pol.Nodes()
            seq = [tri.Node(nodes.Value(k)).Transformed(trsf)
                   for k in range(nodes.Lower(), nodes.Upper() + 1)]
            if edge.Orientation() == TopAbs_REVERSED:
                seq.reverse()
            for p in seq:
                q = (p.X(), p.Y(), p.Z())
                if not pts or pts[-1] != q:
                    pts.append(q)
            wexp.Next()
        if len(pts) > 1 and pts[0] == pts[-1]:
            pts.pop()
        return pts

    def _inner_opening(f):
        """What the largest inner wire of a planar face encloses - the OPENING an
        annular (ring) face rims: {"area_m2", "centroid", "wh_m"}. None for a
        single-wire face (a solid-model disc IS its own opening) and when no inner rim
        is measurable on the triangulation. Largest wins because the biggest hole of a
        flanged end face is the bore; the small ones around it are bolt holes."""
        wires = _wires_of(f)
        if len(wires) < 2:
            return None
        outer_w = BRepTools.OuterWire_s(f)
        best = None
        for wire in wires:
            if wire.IsSame(outer_w):
                continue
            measured = _rim_measure(_rim_polyline(f, wire))
            if measured is None:
                continue
            if best is None or measured["area_m2"] > best["area_m2"]:
                best = measured
        return best

    wall_idx: list[int] = []
    open_idx: list[int] = []
    for i, f in enumerate(faces):
        is_open = (i in opening_faces) if opening_faces is not None \
            else (BRepAdaptor_Surface(f).GetType() == GeomAbs_Plane)
        (open_idx if is_open else wall_idx).append(i)

    if (declared_ports and opening_faces is None
            and len(open_idx) > len(declared_ports) >= 2):
        # More flat faces than declared ports: a box duct's walls are as planar as its ends,
        # and calling them all openings left the wall with zero triangles. The DECLARATION
        # says which ones are real - select those, wall the rest.
        cands = [(i, *_face_props(faces[i]), _inner_opening(faces[i])) for i in open_idx]
        keep = set(select_declared_openings(cands, declared_ports))
        wall_idx.extend(i for i in open_idx if i not in keep)
        open_idx = [i for i in open_idx if i in keep]
        logger.info("tessellate_internal: declaration selected %d of %d planar faces as "
                    "openings; %d fold to the wall", len(open_idx), len(cands),
                    len(cands) - len(open_idx))
    if len(open_idx) < 2:
        raise RuntimeError(
            f"internal-flow geometry needs >=2 flat openings (inlet+outlet); found "
            f"{len(open_idx)}. If the walls are planar (box duct), pass opening_faces "
            f"explicitly.")
    # EVERY flat opening is a port. Keeping only the two largest and folding the rest into the wall
    # SEALS them, and nothing downstream can tell: a Y-junction manifold came back with one branch
    # capped shut, meshed 1,659,660 cells, and was reported production-grade. The mesh was
    # physically wrong - flow could only leave through one branch - and looked fine.
    open_idx.sort(key=lambda i: _face_props(faces[i])[0], reverse=True)

    if len(open_idx) == 2:
        # A plain duct: which end is the inlet is arbitrary by area, so keep the original rule and
        # order the two ports along the axis of greatest separation.
        ca = _face_props(faces[open_idx[0]])[1]
        cb = _face_props(faces[open_idx[1]])[1]
        axis = max(range(3), key=lambda k: abs(ca[k] - cb[k]))
        ports = list(open_idx) if ca[axis] <= cb[axis] else [open_idx[1], open_idx[0]]
        inlet_i, outlet_ids = ports[0], [ports[1]]
    else:
        # A manifold. Area is the only signal available here - the brief names the patches, but this
        # runs before any of that reaches us - so the widest opening is taken as the feed. That is a
        # GUESS and it is wrong whenever the part diffuses or combines: a 40 mm feed into two 60 mm
        # branches labels a branch the inlet and the feed an outlet, and a solver run on it has the
        # flow backwards. The caller states the assumption to the user; the real fix is to bind the
        # brief's declared roles to these ports instead of inferring one.
        inlet_i, outlet_ids = open_idx[0], list(open_idx[1:])

    def _write_group(idxs, path, extra_faces=()):
        comp = TopoDS_Compound(); bld = BRep_Builder(); bld.MakeCompound(comp)
        for i in idxs:
            bld.Add(comp, faces[i])
        for xf in extra_faces:
            bld.Add(comp, xf)
        w = StlAPI_Writer(); w.ASCIIMode = False
        w.Write(comp, str(path))

    def _cap_for_wire(pln, wire):
        # the host orients its hole wire so material lies OUTSIDE it; a face built
        # from that orientation can come out empty, so fall back to the reversal
        for cand_wire in (wire, TopoDS.Wire_s(wire.Reversed())):
            mk = BRepBuilderAPI_MakeFace(pln, cand_wire, True)
            if not mk.IsDone():
                continue
            cand = mk.Face()
            g = GProp_GProps(); BRepGProp.SurfaceProperties_s(cand, g)
            if g.Mass() <= 0:
                continue
            BRepMesh_IncrementalMesh(cand, lin, False, angular_deflection, True)
            if _triangles_of(cand):
                return cand
        return None

    def _owner(face):
        # the solid a face bounds (None for a face of no solid)
        for s in solids:
            fe = TopExp_Explorer(s, TopAbs_FACE)
            while fe.More():
                if fe.Current().IsSame(face):
                    return s
                fe.Next()
        return None

    def _less_other_solids(cap, host):
        # THE LID IS WHAT IS OPEN: the bore less every other solid's footprint in its plane - a
        # centre rod ending there (or running through it) fills the middle, and a lid over the
        # rod's own end face would lay the port patch on top of the wall. OpenCASCADE takes off
        # what lies IN or ON each solid; the faces left are the lid. Returns (faces, area taken).
        if len(solids) < 2:
            return [cap], 0.0
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut

        g = GProp_GProps(); BRepGProp.SurfaceProperties_s(cap, g)
        before = g.Mass()
        cbox = Bnd_Box(); BRepBndLib.Add_s(cap, cbox)
        result = cap
        ends: set = set()
        for s in solids:
            if host is not None and s.IsSame(host):
                continue
            sbox = Bnd_Box(); BRepBndLib.Add_s(s, sbox)
            if sbox.IsOut(cbox):
                continue
            # the solid's own faces lying in the lid, wholly inside the bore: its end faces at
            # the mouth (they are wall no longer once the lid is cut round them - see below)
            fe = TopExp_Explorer(s, TopAbs_FACE)
            while fe.More():
                sf = TopoDS.Face_s(fe.Current())
                fe.Next()
                if BRepAdaptor_Surface(sf).GetType() != GeomAbs_Plane:
                    continue
                gs = GProp_GProps(); BRepGProp.SurfaceProperties_s(sf, gs)
                common = BRepAlgoAPI_Common(sf, cap)
                if not common.IsDone():
                    continue
                gc = GProp_GProps(); BRepGProp.SurfaceProperties_s(common.Shape(), gc)
                if gs.Mass() > 0 and gc.Mass() >= 0.99 * gs.Mass():
                    ends.update(i for i, f in enumerate(faces) if f.IsSame(sf))
            cut = BRepAlgoAPI_Cut(result, s)
            if not cut.IsDone():
                # half a cut is worse than none: the whole lid stays as it was, and so does the wall
                logger.error("tessellate_internal: could not take a solid's footprint off a port "
                             "lid - the lid spans the whole bore, as for a single solid")
                return [cap], 0.0
            result = cut.Shape()
        g = GProp_GProps(); BRepGProp.SurfaceProperties_s(result, g)
        taken = max(before - g.Mass(), 0.0)
        if taken <= 1e-6 * before:
            return [cap], 0.0
        left = []
        fe = TopExp_Explorer(result, TopAbs_FACE)
        while fe.More():
            lf = TopoDS.Face_s(fe.Current())
            gl = GProp_GProps(); BRepGProp.SurfaceProperties_s(lf, gl)
            if gl.Mass() > 1e-9 * before:
                BRepMesh_IncrementalMesh(lf, lin, False, angular_deflection, True)
                if _triangles_of(lf):
                    left.append(lf)
            fe.Next()
        if left:
            lidded_ends.update(ends)  # a bore filled whole has no lid to meet them: they stay
        return left, taken

    port_bored: set = set()           # port faces whose bore holes were lidded
    port_filled: set = set()          # ...of which another solid fills part, room left round it
    lidded_ends: set = set()          # other solids' faces lying in a lid, inside its bore

    def _mouth_caps(port_i):
        """Cap face(s) that SEAL a hollow part's port mouth.

        A solid-model duct's port face is a single-wire disc that already spans the
        opening. A HOLLOW part's (thin-shell wall) declared port face is the metal's
        ANNULAR end ring: its inner wire bounds the bore hole, and writing only the ring
        leaves that hole OPEN in the port STL - the channel then connects to the exterior
        void through the mouth, the snappy carve keeps channel+exterior as ONE region,
        and the delivered mesh grows a spurious 'outer' (blockMesh-skin) patch (jobs
        95bd0197, 0de57541). Every inner wire is closed with a planar face built on the
        ring's own plane and tessellated like the rest, so the port STL spans the whole
        opening. A single-wire face yields no caps - solid-model inputs (the y_duct
        class) come out byte-identical.
        """
        f = faces[port_i]
        wires = _wires_of(f)
        if len(wires) < 2:
            return []
        outer_w = BRepTools.OuterWire_s(f)
        pln = BRepAdaptor_Surface(f).Plane()
        host = _owner(f)
        caps = []
        for wire in wires:
            if wire.IsSame(outer_w):
                continue
            cap = _cap_for_wire(pln, wire)
            if cap is None:
                logger.error(
                    "tessellate_internal: could not build a sealing cap for an inner "
                    "wire of port face %d - the port STL leaves the mouth open and the "
                    "internal carve may keep the exterior void (finalize flags it)",
                    port_i)
                continue
            port_bored.add(port_i)
            left, taken = _less_other_solids(cap, host)
            if taken > 0.0 and left:
                port_filled.add(port_i)
                logger.info("tessellate_internal: port face %d - another solid fills %.0f mm^2 of "
                            "its bore; the lid is the %d face(s) left open", port_i, taken * 1e6,
                            len(left))
            elif taken > 0.0:
                logger.warning("tessellate_internal: port face %d - another solid fills its whole "
                               "bore; nothing is left open to lid", port_i)
            caps.extend(left)
        if caps:
            logger.info("tessellate_internal: annular port face %d - sealed %d bore "
                        "hole(s) so the port STL closes the mouth", port_i, len(caps))
        return caps

    # #
    # UNDECLARED OPENINGS. The mouth caps above seal the DECLARED ports' bore holes.
    # A part can hold MORE openings than the declaration names - a pump housing's rotor
    # bore left open with the rotor removed, a stubbed side port nobody declared, a
    # plain hole drilled through the wall. For an internal-flow carve the fluid may
    # exit ONLY through declared ports, so every other opening of the shell must be
    # sealed, and its seal belongs to the WALL patch (the fluid sees a wall there),
    # never to a port. Detection is geometry-property-based, per inner wire of every
    # wall face: it is an opening only when (a) nothing fills the gap - points just off
    # both sides of the hole's span are outside the solid (a protruding boss/stub fused
    # over the hole fails this and is left alone: capping a filled junction would wall
    # off a live passage, the sealed-branch failure the manifold work documents), and
    # (b) the gap sees the exterior - a straight ray from the hole along its normal
    # leaves the part without re-entering it (a perforated internal baffle fails this
    # and is left alone; its holes join fluid to fluid, not fluid to exterior) - and
    # (c) it sees the exterior DIRECTLY, not through a declared port's mouth. An orifice
    # plate's bore is a hole in a wall face whose axial ray runs down the pipe and out
    # of the open end: void all the way, so (a) and (b) both pass, and every orifice
    # shape in the corpus had its bore capped shut (jobs 848d9dba, 6edbafb2, 1b20782b -
    # mesh a 7 mm slab, real ports sealed over). A ray that exits by crossing a mouth
    # disc is the passage the fluid is meant to take; the mouth is a patch, not a leak.
    # #
    def _gap_is_void(pts, c, n, eps):
        # nothing fills the hole: just off BOTH sides of its span there is no material.
        # Near-rim samples matter - a plug (fused stub/boss) always has material right
        # inside the rim on its side, whatever its bore leaves open at the centre.
        step = max(1, len(pts) // 12)
        samples = [c] + [tuple(p[k] + 0.15 * (c[k] - p[k]) for k in range(3))
                         for p in pts[::step]]
        return not any(
            _inside(tuple(s[k] + sign * eps * n[k] for k in range(3)))
            for s in samples for sign in (1.0, -1.0))

    def _sees_exterior(c, n, eps):
        # a straight all-void ray from the hole to past the part, along either normal -
        # and past the part DIRECTLY, not out through a declared mouth (see (c) above)
        for sign in (1.0, -1.0):
            origin = (c[0] + sign * eps * n[0], c[1] + sign * eps * n[1],
                      c[2] + sign * eps * n[2])
            direction = (sign * n[0], sign * n[1], sign * n[2])
            ray = BRepIntCurveSurface_Inter()
            ray.Init(shape, gp_Lin(gp_Pnt(*origin), gp_Dir(*direction)), 1e-9)
            blocked = False
            while ray.More():
                if ray.W() > 0.1 * eps:      # a forward hit; behind-the-start hits are
                    blocked = True           # the hole's own host surface
                    break
                ray.Next()
            if not blocked and not _exits_through_mouth(origin, direction):
                return True
        return False

    def _exits_through_mouth(origin, direction) -> bool:
        # The ray leaves the part by crossing a declared port's own mouth disc. That is
        # the fluid's passage, and the mouth is capped as a patch downstream - so what the
        # hole "sees" is the port, never the exterior. The frames are built once the ports
        # are chosen (mouth_frames below); this runs only from the sealing pass after that.
        for pc, pn, pr in mouth_frames:
            if pr <= 0.0:
                continue
            denom = sum(direction[k] * pn[k] for k in range(3))
            if abs(denom) < 1e-9:
                continue                     # travelling along the mouth plane
            t = sum((pc[k] - origin[k]) * pn[k] for k in range(3)) / denom
            if t <= 0.0:
                continue                     # the mouth is behind the ray
            hit = [origin[k] + t * direction[k] for k in range(3)]
            lateral = _m.sqrt(sum((hit[k] - pc[k]) ** 2 for k in range(3)))
            if lateral <= 1.05 * pr:
                return True
        return False

    # #
    # PORT-MOUTH NEIGHBOURHOOD. A flanged mouth is rimmed by SEVERAL coaxial ring faces:
    # the duct wall's end ring (the chosen port face) plus flange annuli around and just
    # behind the very same hole. Once the port face is chosen the flange faces are wall,
    # and each one's inner wire still reads as an opening to the probes below - nothing
    # fills it and it sees the exterior, because it IS the port's own mouth seen through
    # the flange stack. Sealing it would cap the declared opening into the wall: flow
    # shut at its own inlet. A wall-face hole belongs to a mouth, not to an undeclared
    # opening, when its plane is parallel to the port's, it sits on the port's axis
    # within a quarter mouth radius (axially and laterally), and it is mouth-sized
    # (>= 3/4 of the mouth radius). A bolt hole beside the bore fails the lateral test
    # and a small tap at the mouth fails the size test - both stay sealable.
    # #
    mouth_frames: list = []
    for pi in (inlet_i, *outlet_ids):
        pf = faces[pi]
        prof = _inner_opening(pf)
        if prof is not None:
            m_c, m_area = prof["centroid"], prof["area_m2"]
        else:
            m_area, m_c = _face_props(pf)
        m_ax = BRepAdaptor_Surface(pf).Plane().Axis().Direction()
        mouth_frames.append((tuple(m_c), (m_ax.X(), m_ax.Y(), m_ax.Z()),
                             _m.sqrt(max(m_area, 0.0) / _m.pi)))

    def _clear_line(a, b) -> bool:
        # The straight segment between two points crosses no material - the two holes
        # are joined by an unobstructed void passage (a bore). Endpoints excluded:
        # each sits on its own host surface.
        d = [b[k] - a[k] for k in range(3)]
        length = _m.sqrt(sum(v * v for v in d))
        if length <= 0.0:
            return True
        dn = [v / length for v in d]
        ray = BRepIntCurveSurface_Inter()
        ray.Init(shape, gp_Lin(gp_Pnt(*a), gp_Dir(*dn)), 1e-9)
        while ray.More():
            if 1e-6 * length < ray.W() < length * (1.0 - 1e-3):
                return False
            ray.Next()
        return True

    def _is_port_mouth(c, n, hole_area) -> bool:
        r_hole = _m.sqrt(max(hole_area, 0.0) / _m.pi)
        for pc, pn, pr in mouth_frames:
            if pr <= 0.0:
                continue
            if abs(sum(n[k] * pn[k] for k in range(3))) < 0.99:
                continue
            off = [c[k] - pc[k] for k in range(3)]
            axial = sum(off[k] * pn[k] for k in range(3))
            lateral = _m.sqrt(max(sum(v * v for v in off) - axial * axial, 0.0))
            if lateral > 0.25 * pr or r_hole < 0.75 * pr:
                continue
            # Same axis, mouth-sized. The flange-ring case sits within a quarter radius
            # of the mouth. The JUNCTION case does not: a ported chamber's inner wall
            # has this hole a whole tube-length behind the mouth (mini_housing - eleven
            # baseline episodes died with every chamber-to-bore junction sealed as an
            # "undeclared opening"; the void/exterior probes cannot tell, because a
            # bore is void rather than material and its exit ray leaves through the
            # declared mouth itself). The discriminator is the PASSAGE: when the
            # segment from this hole to the mouth is pure void, the hole opens into
            # the declared port's own bore and is never sealable. A bolt hole fails
            # the lateral test, a small tap fails the size test, and an unrelated
            # coaxial hole is separated from the mouth by material.
            if abs(axial) <= 0.25 * pr or _clear_line(c, pc):
                return True
        return False

    undeclared_caps: list = []        # OCC cap faces (planar hosts) -> the wall group
    undeclared_membranes: list = []   # raw membrane triangles (curved hosts) -> wall.stl
    sealed_openings: list[dict] = []
    for wi in wall_idx:
        wf = faces[wi]
        wires = _wires_of(wf)
        if len(wires) < 2:
            continue                  # no holes - the overwhelmingly common wall face
        outer_w = BRepTools.OuterWire_s(wf)
        host = BRepAdaptor_Surface(wf)
        host_planar = host.GetType() == GeomAbs_Plane
        for wire in wires:
            if wire.IsSame(outer_w):
                continue
            pts = _rim_polyline(wf, wire)
            if len(pts) < 3:
                continue
            frame = _newell_frame(pts)
            if frame is None:
                continue
            n, c = frame
            hole = _rim_measure(pts)
            if hole is not None and _is_port_mouth(c, n, hole["area_m2"]):
                logger.info(
                    "tessellate_internal: face %d's inner wire (%.0f mm^2 at "
                    "(%.3f, %.3f, %.3f) m) rims a declared port mouth - it is the "
                    "port's to cap, not an undeclared opening", wi,
                    hole["area_m2"] * 1e6, *c)
                continue
            perim = sum(_m.dist(pts[i], pts[(i + 1) % len(pts)])
                        for i in range(len(pts)))
            eps = 0.1 * perim / (2.0 * _m.pi)       # a tenth of the hole's own radius
            if not (_gap_is_void(pts, c, n, eps) and _sees_exterior(c, n, eps)):
                continue
            cap = _cap_for_wire(host.Plane(), wire) if host_planar else None
            if cap is not None:
                g = GProp_GProps(); BRepGProp.SurfaceProperties_s(cap, g)
                area = g.Mass()
                undeclared_caps.append(cap)
            else:
                membrane = _ring_membrane(pts)
                if not membrane:
                    logger.error(
                        "tessellate_internal: face %d holds an undeclared opening whose "
                        "rim could not be sealed - the internal carve may keep the "
                        "exterior void (finalize flags it)", wi)
                    continue
                area = sum(_tri_area(t) for t in membrane)
                undeclared_membranes.extend(membrane)
            sealed_openings.append({
                "face": wi, "area": round(area, 8),
                "centroid": [round(v, 6) for v in c]})
            logger.warning(
                "tessellate_internal: face %d holds an opening that is no declared port "
                "(%.0f mm^2 at (%.3f, %.3f, %.3f) m) - sealed into the WALL: an "
                "internal-flow fluid may exit only through declared ports",
                wi, area * 1e6, *c)

    # one patch per outlet, so a branch can carry its own boundary condition and its own flow split
    outlet_names = (["outlet"] if len(outlet_ids) == 1
                    else [f"outlet_{n}" for n in range(1, len(outlet_ids) + 1)])
    stls = {"wall": out_dir / "wall.stl", "inlet": out_dir / "inlet.stl"}
    for nm in outlet_names:
        stls[nm] = out_dir / f"{nm}.stl"
    port_caps = {pi: _mouth_caps(pi) for pi in (inlet_i, *outlet_ids)}

    # verified interior point for locationInMesh. WHICH region is the flow comes first:
    #   * a declared fluid domain, or an undeclared solid: the solid IS the flow (a duct modeled
    #     as a rod, an annular passage), and a point inside it is in the flow;
    #   * a solid declared a BODY whose ports are rings with capped bores: the solid is the
    #     METAL of a hollow wall, and the flow is the cavity the wall closes with its port caps.
    #     No point inside the solid is ever the seed there - a thick part's volume centroid
    #     sits in its metal, and a carve seeded there meshes the wall whole, with no stray
    #     patch to give it away;
    #   * a body whose ports are plain discs is a rod the user called a body: the solid again;
    #   * a file that does not say, whose port bores hold ANOTHER solid with room left round it
    #     (a centre rod, an insert): the flow is that room between the solids, so every solid
    #     is wall - read as the solids themselves, the seed landed in the pipe's metal.
    hollow_wall = bool(port_bored) and (
        fluid_solid is False or (fluid_solid is None and bool(port_filled)))
    if hollow_wall and lidded_ends:
        # The other solids' end faces a lid was cut round are no boundary of the flow: the flow
        # stops at the lid, and those faces only close the solid off from the outside. Kept, they
        # close it into a region of its own - cfMesh meshed annular_001's rod (the largest
        # closed region), not the annulus round it. The lid and the solid's side wall meet edge
        # to edge without them.
        wall_idx = [i for i in wall_idx if i not in lidded_ends]
    _write_group(wall_idx, stls["wall"], extra_faces=undeclared_caps)
    _write_group([inlet_i], stls["inlet"], extra_faces=port_caps[inlet_i])
    for nm, oi in zip(outlet_names, outlet_ids):
        _write_group([oi], stls[nm], extra_faces=port_caps[oi])
    if undeclared_membranes:
        write_stl_binary(stls["wall"],
                         read_stl_triangles(stls["wall"]) + undeclared_membranes)

    # RIM AUDIT - the staged surface as a whole must be closed. A face OCC skipped
    # ("null triangulation": the fda pump housing's 0.5 mm blend sliver) leaves a gap no
    # B-rep wire scan can see: the B-rep is watertight, the TRIANGLES are not, and the
    # carve leaks through the gap into the exterior void. Read back what was written
    # (file space, so vertices compare exactly), seal every open rim ring into the wall.
    staged = {nm: read_stl_triangles(p) for nm, p in stls.items()}
    for nm in stls:
        if nm != "wall" and not staged[nm]:
            raise RuntimeError(
                f"internal-flow port {nm!r} tessellated to zero triangles - its face "
                f"cannot become a boundary patch (geometry too degenerate to mesh?)")
    rim_membranes, open_rims = seal_open_rims(
        [t for ts in staged.values() for t in ts])
    if rim_membranes:
        logger.error(
            "tessellate_internal: staged surface has %d open rim ring(s) - a face the "
            "tessellator skipped left a gap; sealed into the WALL so the carve cannot "
            "reach the exterior void: %s",
            sum(1 for r in open_rims if r["sealed"]),
            "; ".join(f"{r['edges']} edges at {tuple(r['centroid'])} m"
                      for r in open_rims))
        write_stl_binary(stls["wall"], staged["wall"] + rim_membranes)
    if any(not r["sealed"] for r in open_rims):
        logger.error(
            "tessellate_internal: %d boundary component(s) could not be sealed - the "
            "internal carve may keep the exterior void (finalize flags it)",
            sum(1 for r in open_rims if not r["sealed"]))

    # (which region is the flow - hollow_wall - is read above, before the groups were written)

    def _plane_basis(n):
        # two in-plane unit vectors: any vector not parallel to n, made orthogonal, and n x it
        seed_u = [1.0, 0.0, 0.0] if abs(n[0]) < 0.9 else [0.0, 1.0, 0.0]
        dot = sum(seed_u[k] * n[k] for k in range(3))
        u = [seed_u[k] - dot * n[k] for k in range(3)]
        ul = _m.sqrt(sum(v * v for v in u)) or 1.0
        u = [v / ul for v in u]
        v = [n[1] * u[2] - n[2] * u[1], n[2] * u[0] - n[0] * u[2], n[0] * u[1] - n[1] * u[0]]
        return u, v

    interior: tuple | None = None
    if not hollow_wall:
        # Candidates, cheapest-first: each solid's volume centroid, then each port centroid nudged
        # inward along its (oriented) normal.
        candidates = []
        for s in solids:
            gv = GProp_GProps(); BRepGProp.VolumeProperties_s(s, gv)
            vc = gv.CentreOfMass()
            candidates.append((vc.X(), vc.Y(), vc.Z()))
        for pi in (inlet_i, *outlet_ids):
            f = faces[pi]
            area, c = _face_props(f)
            ax = BRepAdaptor_Surface(f).Plane().Axis().Direction()
            n = [ax.X(), ax.Y(), ax.Z()]
            if f.Orientation() == TopAbs_REVERSED:
                n = [-v for v in n]
            step = 0.5 * _m.sqrt(area / _m.pi)              # ~half the port radius, inward
            candidates.append(tuple(c[k] - step * n[k] for k in range(3)))
            candidates.append(tuple(c[k] + step * n[k] for k in range(3)))
        # RING PORTS. The centroid of an annular port face is the centre of the hole it rims -
        # for a blade-row passage that is the hub bore, which is not fluid. So every ring port
        # also offers points ON the ring: mid-radius, four in-plane directions, nudged inward
        # along the port normal. Blade-row passages 001/003/005 (jobs 9bf37dd8, f69eb843,
        # db9e64e1) were "delivered" as a mesh of the hub bore - the port caps sealed the bore
        # at both ends, the seed sat inside it, and snappyHexMesh kept it. NOT for a solid
        # declared a body (fluid_solid is False, which is what the driver passes for every
        # non-fluid input): a metal tube's end face is an annulus too, and there the ring IS
        # the wall - a point on it would seed the metal, not the bore. Undeclared keeps the
        # primary semantics: the solid is the fluid.
        for pi in ((inlet_i, *outlet_ids) if fluid_solid is not False else ()):
            f = faces[pi]
            prof = _inner_opening(f)
            if prof is None:
                continue
            area, c = _face_props(f)
            inner_area = float(prof["area_m2"])
            r_out = _m.sqrt(max(area + inner_area, 0.0) / _m.pi)
            r_in = _m.sqrt(max(inner_area, 0.0) / _m.pi)
            r_mid = 0.5 * (r_out + r_in)
            ax = BRepAdaptor_Surface(f).Plane().Axis().Direction()
            n = [ax.X(), ax.Y(), ax.Z()]
            u, v = _plane_basis(n)
            step = 0.5 * (r_out - r_in)
            for d in (u, [-x for x in u], v, [-x for x in v]):
                on_ring = [c[k] + r_mid * d[k] for k in range(3)]
                for sign in (-1.0, 1.0):
                    candidates.append(tuple(on_ring[k] + sign * step * n[k] for k in range(3)))
        # THE SOLID IS THE FLOW - but which, when there are several? The one the ports belong to:
        # an annular fluid with a separate insert down its middle is the annulus, and its centroid,
        # on the axis, lies in the insert. A point in a solid that carries no port is in a body.
        owners = [o for o in (_owner(faces[pi]) for pi in (inlet_i, *outlet_ids)) if o is not None]
        flow_cls = [c for s, c in zip(solids, classifiers) if any(s.IsSame(o) for o in owners)] or classifiers
        body_cls = [c for c in classifiers if all(c is not f for f in flow_cls)]

        def _in_flow(p) -> bool:
            def _in(cls_list) -> bool:
                for cls in cls_list:
                    cls.Perform(gp_Pnt(*p), 1e-9)
                    if cls.State() == TopAbs_IN:
                        return True
                return False
            return _in(flow_cls) and not _in(body_cls)

        interior = next((p for p in candidates if _in_flow(p)), None)
    if interior is None and fluid_solid:
        # A DECLARED fluid domain is the fluid: a point that is not inside the solid is not in
        # the flow, whatever the hollow-wall search below would make of it. Refuse loudly
        # rather than seed a void.
        raise RuntimeError(
            "could not locate a point inside the declared fluid domain for locationInMesh - "
            "the volume centroid, the port centroids and the ring-port candidates all fall "
            "outside the solid (a hole through the part?); the mesh would have been of the "
            "void, not the flow")

    def _mouth_probe_rings():
        # One ring of (point, the mouth point it is reached from, the mouth's radius) per port
        # and depth, best first. Each port's mouth - the centre of the opening its ring rims,
        # or of the disc itself - is stepped along the port axis at decreasing depth, on the
        # axis and then off it, out to 0.95 of the radius: a centre body can fill the axis and
        # most of the mouth (the annular corpus family's rods span 0.61-0.90 of the bore).
        # BOTH ways along the axis: which side is in is for the verification to prove, never
        # for a normal's sign to assume.
        offsets = [(0.0, 0.0)] + [(f * a, f * b) for f in (0.5, 0.8, 0.9, 0.95)
                                  for a, b in ((1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0))]
        for pi in (inlet_i, *outlet_ids):
            f = faces[pi]
            prof = _inner_opening(f)
            if prof is not None:
                c, area = tuple(prof["centroid"]), float(prof["area_m2"])
            else:
                area, c = _face_props(f)
            r = _m.sqrt(max(area, 0.0) / _m.pi)
            ax = BRepAdaptor_Surface(f).Plane().Axis().Direction()
            n = [ax.X(), ax.Y(), ax.Z()]
            u, v = _plane_basis(n)
            for depth in (0.5, 0.3, 0.15):
                ring = []
                for a, b in offsets:
                    mouth = tuple(c[k] + r * (a * u[k] + b * v[k]) for k in range(3))
                    for sign in (1.0, -1.0):
                        ring.append((tuple(mouth[k] + sign * depth * r * n[k]
                                           for k in range(3)), mouth, r))
                yield ring

    if interior is None:
        # THE CAPPED CAVITY. A real machined part - a rocket nozzle, a flanged elbow - is METAL
        # with a channel through it, and the flow is the region its wall closes with the port
        # caps. The old search stepped from the inlet toward the outlet and took the first
        # point not in the metal. On a bent duct that line leaves the channel at once:
        # bend_elbow_021 (a 162.64-degree elbow, the outlet almost beside the inlet) was seeded
        # 0.5 mm upstream of its own inlet cap - not in the metal, but in the exterior - and
        # the carve kept the exterior void. A point is taken here only when it is PROVEN in the
        # cavity, against the surface the mesher reads: not in the metal, reached from a port's
        # cap in a straight line that crosses nothing, and closed in - every ray from it stops
        # on the staged surface, where from the exterior some ray reaches open space. The cap
        # divides exactly those two regions, so a point that passes is in the flow. The metal
        # is EVERY solid of the file: a centre rod modelled as a solid of its own (annular_004)
        # is closed in by the staged surface too, and the first solid alone would not see it.
        metal = []
        se = TopExp_Explorer(shape, TopAbs_SOLID)
        while se.More():
            metal.append(BRepClass3d_SolidClassifier(TopoDS.Solid_s(se.Current())))
            se.Next()

        def _in_metal(p) -> bool:
            for cls in metal:
                cls.Perform(gp_Pnt(*p), 1e-9)
                if cls.State() == TopAbs_IN:
                    return True
            return False

        carve = CarveRays([t for p in stls.values() for t in read_stl_triangles(p)])
        best: tuple | None = None
        for ring in _mouth_probe_rings():
            for cand, mouth, r_mouth in ring:
                p = tuple(round(x, 6) for x in cand)        # judged as it is written
                if _in_metal(p) or not carve.clear_between(mouth, p):
                    continue
                clearance = carve.enclosed_clearance(p)
                if clearance is None or clearance < 0.02 * r_mouth:
                    continue                                 # open space, or on a wall
                if best is None or clearance > best[1]:
                    best = (p, clearance)
                if clearance >= 0.25 * r_mouth:
                    break                                    # well clear of every wall
            if best is not None:
                break          # the clearest proven point of the first ring that holds one
        if best is not None:
            interior = best[0]
            logger.info(
                "tessellate_internal: seed (%.6f, %.6f, %.6f) m proven in the capped cavity - "
                "outside the metal, straight in from a port cap, closed in on every ray "
                "(nearest surface %.1f mm)", *interior, best[1] * 1000.0)
    if interior is None:
        raise RuntimeError(
            "could not locate a point in the flow for locationInMesh - every point tried is in "
            "the metal or reaches open space, so the ports do not open into a cavity the wall "
            "closes (an opening missing from the declaration, or a wall that is not closed?)")

    n_wall_faces = sum(len(read_stl_triangles(stls["wall"])) for _ in [0])

    def _opening_record(pi):
        # the face's own measure, plus - for an annular (ring) port face - what its inner
        # wire encloses: the OPENING the declaration talks about, which downstream size
        # matching may use where the ring's metal area cannot (engines/port_binding)
        rec = {"area": round(_face_props(faces[pi])[0], 8),
               "centroid": [round(v, 6) for v in _face_props(faces[pi])[1]]}
        prof = _inner_opening(faces[pi])
        if prof is not None:
            rec["opening"] = {"area": round(prof["area_m2"], 8),
                              "centroid": [round(v, 6) for v in prof["centroid"]],
                              "wh": [round(v, 6) for v in prof["wh_m"]]}
            filled = _filled_area(pi, prof)
            if filled > 0.0:
                # a body standing in the bore (a centre rod) shows a flat face inside the
                # inner wire: the flow crosses the opening less that face (port_binding)
                rec["opening"]["filled"] = round(filled, 8)
        return rec

    def _filled_area(pi, prof) -> float:
        """Area of the other flat faces lying inside a ring face's inner wire, in its plane."""
        oc = prof["centroid"]
        wh = prof.get("wh_m") or ()
        reach = (0.5 * min(wh) if len(wh) == 2 and min(wh) > 0
                 else _m.sqrt(float(prof["area_m2"]) / _m.pi))
        ax = BRepAdaptor_Surface(faces[pi]).Plane().Axis().Direction()
        n = (ax.X(), ax.Y(), ax.Z())
        total = 0.0
        for j, fj in enumerate(faces):
            if j == pi or BRepAdaptor_Surface(fj).GetType() != GeomAbs_Plane:
                continue
            a, c = _face_props(fj)
            if a >= float(prof["area_m2"]):
                continue
            off = [c[k] - oc[k] for k in range(3)]
            if (abs(sum(off[k] * n[k] for k in range(3))) <= 1e-3 * reach
                    and _m.sqrt(sum(v * v for v in off)) < reach):
                total += a
        return total

    return {
        "stls": {k: str(v) for k, v in stls.items()},
        "interior_point": [round(v, 6) for v in interior],
        "bbox_min": [round(v, 6) for v in (x0, y0, z0)],
        "bbox_max": [round(v, 6) for v in (x1, y1, z1)],
        "openings": {
            "inlet": _opening_record(inlet_i),
            **{nm: _opening_record(oi) for nm, oi in zip(outlet_names, outlet_ids)}},
        "n_wall_faces": n_wall_faces,
        # what was sealed into the wall beyond the declared ports, for manifests and
        # user-facing evidence: undeclared shell openings (B-rep holes nothing fills)
        # and open rim rings (gaps the tessellator itself left in the staged surface)
        "sealed": {"undeclared_openings": sealed_openings, "open_rims": open_rims},
    }


def tessellate_regions_to_stl(geom_path, out_stl, *, prepared=None, angular_deflection: float = 0.2,
                              linear_deflection: float | None = None) -> list[str]:
    # Each named component tessellated into its own STL solid; returns the names written.
    #
    # The flat path above writes OneShape(), which fuses every component into one unnamed solid.
    # That is right for a body meshed as a single wall, and lossy for a surface whose parts are
    # named - and the loss happens here, before any engine could have chosen. A file that
    # distinguishes nothing takes the flat path unchanged and reports no names.
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.BRepMesh import BRepMesh_IncrementalMesh

    from meshpipeline.cad.regions import components_of, regions_of
    from meshpipeline.cad.stl_io import write_stl_solids

    geom_path, out_stl = Path(geom_path), Path(out_stl)

    def _flat() -> list[str]:
        tessellate_to_stl(geom_path, out_stl, prepared=prepared,
                          angular_deflection=angular_deflection,
                          linear_deflection=linear_deflection)
        return []

    if regions_of(geom_path).count < 2:
        return _flat()

    _doc, tool, found, _roots = components_of(geom_path)
    trsf = _occ_to_metres(prepared)
    solids: dict[str, list] = {}
    for index, (label, name) in enumerate(found):
        shape = tool.GetShape_s(label)
        if shape is None or shape.IsNull():
            continue
        shape = BRepBuilderAPI_Transform(shape, trsf, True).Shape()
        box = Bnd_Box()
        BRepBndLib.Add_s(shape, box)
        x0, y0, z0, x1, y1, z1 = box.Get()
        diag = ((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2) ** 0.5
        lin = linear_deflection if linear_deflection is not None else max(diag / 2500.0, 1e-9)
        BRepMesh_IncrementalMesh(shape, lin, False, angular_deflection, True)
        tris = _triangles_of(shape)
        if tris:
            solids[name or f"region_{index + 1}"] = tris
    if len(solids) < 2:
        # Fewer than two solids survived tessellation, so there is nothing to keep apart.
        return _flat()
    write_stl_solids(out_stl, solids)
    return list(solids)


def _triangles_of(shape) -> list:
    # Every triangulated face of one shape, in world coordinates.
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    out: list = []
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        face = TopoDS.Face_s(explorer.Current())
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is not None:
            transform = loc.Transformation()
            for k in range(1, tri.NbTriangles() + 1):
                a, b, c = tri.Triangle(k).Get()
                pts = []
                for node in (a, b, c):
                    p = tri.Node(node).Transformed(transform)
                    pts.append((float(p.X()), float(p.Y()), float(p.Z())))
                out.append(tuple(pts))
        explorer.Next()
    return out
