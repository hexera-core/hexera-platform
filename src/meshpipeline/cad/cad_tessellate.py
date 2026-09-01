# Responsibility: Turn a CAD solid into a triangulated surface.
# Boundaries: tessellation only; the unit and scale it works in are decided before it runs.
from __future__ import annotations

import logging
import math
from pathlib import Path

from meshpipeline.cad.stl_io import read_stl_triangles

logger = logging.getLogger(__name__)


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
    if geom_path.suffix.lower() in (".igs", ".iges"):
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

    def _size_err(area, opening, port):
        # the smallest relative disagreement any admissible measure achieves inside the
        # band, or None when the declared size fits by neither measure
        declared_area = float(port["area_m2"])
        errs = []
        if _band_ok(area, declared_area):
            errs.append(abs(area / declared_area - 1.0))
        if (opening is not None and opening.get("area_m2")
                and _shape_agrees(opening, port)
                and _band_ok(float(opening["area_m2"]), declared_area)):
            errs.append(abs(float(opening["area_m2"]) / declared_area - 1.0))
        return min(errs) if errs else None

    hinted = sorted((p for p in declared if p.get("near_m")), key=lambda p: str(p.get("name")))
    sized = sorted((p for p in declared if not p.get("near_m")),
                   key=lambda p: str(p.get("name")))
    for port in hinted + sized:
        best = None
        for idx, (area, cen, opening) in remaining.items():
            if port.get("area_m2"):
                err = _size_err(area, opening, port)
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


def tessellate_internal(geom_path, out_dir, *, prepared=None, angular_deflection: float = 0.2,
                        linear_deflection: float | None = None,
                        opening_faces: list[int] | None = None,
                        declared_ports: list | None = None) -> dict:
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
    if geom_path.suffix.lower() in (".igs", ".iges"):
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

    solid_exp = TopExp_Explorer(shape, TopAbs_SOLID)
    if not solid_exp.More():
        raise RuntimeError(
            "internal-flow input is not a watertight SOLID - the fluid volume must be a "
            "closed solid (a loose surface/shell is the pipe skin, not the flow passage)")
    solid = TopoDS.Solid_s(solid_exp.Current())

    def _inside(p) -> bool:
        cls = BRepClass3d_SolidClassifier(solid)
        cls.Perform(gp_Pnt(*p), 1e-9)
        return cls.State() == TopAbs_IN

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
            caps.append(cap)
        if caps:
            logger.info("tessellate_internal: annular port face %d - sealed %d bore "
                        "hole(s) so the port STL closes the full mouth", port_i, len(caps))
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
    # and is left alone; its holes join fluid to fluid, not fluid to exterior).
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
        # a straight all-void ray from the hole to past the part, along either normal
        for sign in (1.0, -1.0):
            start = gp_Pnt(c[0] + sign * eps * n[0], c[1] + sign * eps * n[1],
                           c[2] + sign * eps * n[2])
            ray = BRepIntCurveSurface_Inter()
            ray.Init(shape, gp_Lin(start, gp_Dir(sign * n[0], sign * n[1], sign * n[2])),
                     1e-9)
            blocked = False
            while ray.More():
                if ray.W() > 0.1 * eps:      # a forward hit; behind-the-start hits are
                    blocked = True           # the hole's own host surface
                    break
                ray.Next()
            if not blocked:
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
            if (abs(axial) <= 0.25 * pr and lateral <= 0.25 * pr
                    and r_hole >= 0.75 * pr):
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
    _write_group(wall_idx, stls["wall"], extra_faces=undeclared_caps)
    _write_group([inlet_i], stls["inlet"], extra_faces=_mouth_caps(inlet_i))
    for nm, oi in zip(outlet_names, outlet_ids):
        _write_group([oi], stls[nm], extra_faces=_mouth_caps(oi))
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

    # verified interior point for locationInMesh. Candidates, cheapest-first:
    # volume centroid, then each port centroid nudged inward along its (oriented) normal.
    gv = GProp_GProps(); BRepGProp.VolumeProperties_s(solid, gv)
    vc = gv.CentreOfMass()
    candidates = [(vc.X(), vc.Y(), vc.Z())]
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
    interior = next((p for p in candidates if _inside(p)), None)
    if interior is None:
        # HOLLOW-WALL FALLBACK. Everything above assumes the input solid IS the fluid
        # volume (a duct modeled as a solid rod), where inside-the-solid means inside the
        # flow. A real machined part - a rocket nozzle - is METAL with a channel through
        # it: the channel is NOT inside the solid, so every rod-semantics candidate is
        # correctly rejected and the old code died here. For a hollow part the right test
        # inverts: a point IN the channel is NOT in the metal. Direction is what makes it
        # safe - nudging a port centroid TOWARD THE OTHER PORT walks down the channel by
        # construction (an annular mouth's centroid sits in the void at the channel mouth),
        # so a not-in-metal point found this way is in the flow region, never in the
        # exterior void around the part.
        pc_in = _face_props(faces[inlet_i])[1]
        for oi in outlet_ids:
            pc_out = _face_props(faces[oi])[1]
            seg = [pc_out[k] - pc_in[k] for k in range(3)]
            seg_len = _m.sqrt(sum(v * v for v in seg)) or 1.0
            u = [v / seg_len for v in seg]
            r_in = _m.sqrt(_face_props(faces[inlet_i])[0] / _m.pi)
            r_out = _m.sqrt(_face_props(faces[oi])[0] / _m.pi)
            hollow = []
            for base, direction, radius in ((pc_in, 1.0, r_in), (pc_out, -1.0, r_out)):
                step = min(0.5 * radius, 0.1 * seg_len)
                hollow.append(tuple(base[k] + direction * step * u[k] for k in range(3)))
            hollow.append(tuple(pc_in[k] + 0.5 * seg_len * u[k] for k in range(3)))
            interior = next((p for p in hollow if not _inside(p)), None)
            if interior is not None:
                logger.info("tessellate_internal: hollow-wall fluid point found on the "
                            "inlet-outlet segment (rod-semantics candidates were all "
                            "inside the metal's complement)")
                break
    if interior is None:
        raise RuntimeError("could not locate a point inside the fluid solid for "
                           "locationInMesh (geometry may not be a closed volume)")

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
        return rec

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
