# Responsibility: The STRUCTURED steps of the shell mesh - what makes it look like an engineer's hand-made mesh:
# WASHERS round holes (SHMESH-39: a concentric ring of quads hugging each round hole of a flat face, an even number of
# elements round it, the field mesh growing out from the ring) and MAPPED quads on faces with four natural corners
# (SHMESH-40: a transfinite grid, opposite sides counted alike, pipes as a grid of rows along the axis and columns
# round it). Everything here is planned from the face's own geometry and the sizes the plan already chose.
# Boundaries: works on the gmsh model the shell adapter built (engines/gmsh/shell_adapter.py), in that process;
# reads and writes nothing else. Deterministic: every choice is made in a fixed order with fixed numbers.
# Collaborates with: engines/gmsh/shell_adapter.py (calls it before and after the 2D mesh), engines/shell_structure.py
# (measures the result).
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

#: SHMESH-39: washers round the round holes of flat faces
WASHERS = True
#: a hole gets a washer when it is at most WASHER_ACROSS times its face's element size across (a bigger hole is a
#: cut-out: the field mesh already follows it)
WASHER_ACROSS = 3.0
#: SHMESH-45e: ... and up to WASHER_ACROSS_USER sizes across under the user's own global size (a 46 mm hole at 10 mm:
#: an engineer puts a washer there; the rows then stay inside the user's shortest and longest edge). Kt of a plate with
#: a 20 mm hole at 5 mm (4 sizes across): 2.34 with a washer, 2.36 without, theory 2.51 - the same within 1%.
WASHER_ACROSS_USER = 6.0
#: the elements round a hole: a multiple of WASHER_STEP, at least WASHER_MIN (8, 12, 16 ... as an engineer counts),
#: never fewer than the plan put round it (the hole keeps its shape), at most WASHER_MAX
WASHER_MIN = 8
WASHER_STEP = 4
WASHER_MAX = 36
#: the ring's outer diameter is WASHER_RING times the hole's (an engineer's washer: 1.5 to 2 times); its rows are
#: near-square (each row's width its arc length round), at most WASHER_ROWS rows
WASHER_RING = (1.4, 2.0)
WASHER_ROWS = 2
#: SHMESH-51o25: a washer's rows no narrower than the user's shortest edge where a wider ring fits their longest
RING_AT_LOW = False
#: a ring squeezed by an edge or another hole may shrink to WASHER_SHRUNK times the hole's diameter; past that the hole
#: has no washer
WASHER_SHRUNK = 1.25
#: between a ring and any other edge of its face at least WASHER_ROOM of the ring's outer element length is left for
#: the field mesh
WASHER_ROOM = 0.5
#: SHMESH-51: BOLT HOLES. A round hole that carries load - it lines up with a hole in another plate (a bolt through
#: both), it is one of a pattern of alike holes (a bolt circle, a row), or its plate is joined to another plate - gets
#: BOLT_RINGS rings of quads where they fit inside the user's edges and the room round it (to the next hole, the plate's
#: edge, a joint); where only one fits it gets one and the record says why. Any other hole keeps the ring its size gives.
BOLTS = True
BOLT_RINGS = 2
#: ... and a face whose two-ring holes leave a quality line of the user's unmet on its own elements is meshed again with
#: one ring round them (a plate's mesh changed far from its two bolt holes and a triangle there passed the aspect line)
BOLT_CHECK = True
#: SHMESH-51b: a face's grid comes before a second ring: a face whose grid is refused (by its trial, or by the checks
#: after meshing) while it holds two-ring holes is tried again with one ring round them, and only goes free when the
#: grid fails with one ring too (a channel web's 50 bolt holes took two rings and the web lost its straight rows)
BOLT_GRID_FIRST = True
#: SHMESH-51c: a two-ring bolt hole's free box asks for its band's counts up to the user's longest edge
BOLT_BAND_LONG = True
#: SHMESH-51d: at most this many rounds of holes taken back to one ring, one by one, before the whole face is
BOLT_HOLE_ROUNDS = 3
#: two holes are one bolt's when their axes are parallel within BOLT_AXIS_DEG, each centre lies on the other's axis
#: within BOLT_AXIS_OFF of the smaller radius, their diameters are alike within BOLT_ALIKE, at most BOLT_REACH
#: diameters apart along the axis
BOLT_AXIS_DEG = 2.0
BOLT_AXIS_OFF = 0.1
BOLT_ALIKE = 0.15
BOLT_REACH = 10.0
#: holes of one pattern: one plane, diameters alike within PATTERN_ALIKE; on a circle or in a row within PATTERN_TOL of
#: their own radius
PATTERN_ALIKE = 0.02
#: SHMESH-51o7: a four-sided straight-edged hole the square plan takes (a carriage bolt's) is a bolt hole when it lines
#: up with a hole of about its size in another plate, or is within BOLT_ALIKE of a round bolt hole's size on its plate
#: (its size: its inscribed circle); its square ring then takes two rows where they fit
SQUARE_BOLTS = True
#: SHMESH-51o8: every bolt hole's free box in a face gridded with others along its joints (one ring too) grows no
#: further than keeps its band's corner kites and segments under the user's longest edge: one-ring boxes drawn on to
#: near lines (44.5 to 50.1 mm) refused their bands ("an edge over the longest edge") and were paved, with triangles
#: (in every face it cost a 50 mm pitch web its grid: its boxes could no longer share their lines)
BOLT_BOX_KITE = True
#: SHMESH-51o9: the bolt holes of a face gridded with others along its joints own their boxes too (no line ends on
#: them): a joined plate's two-ring boxes carried their lines across the joint and cut a one-ring box's sides into
#: 10.6 + 34.0 mm, its cell was paved (11 triangles a box)
BOLT_OWN_JOINED = True
PATTERN_TOL = 0.1
#: a bolt hole's rings reach out at most BOLT_RING_MAX times its radius; their outer elements are no longer than the
#: user's longest edge (with none given: BOLT_OUT face sizes, the longest a mapped segment may be); no ring's quad has
#: one side more than BOLT_ASPECT times another
BOLT_RING_MAX = 3.0
BOLT_OUT = 1.4
BOLT_ASPECT = 3.0
#: SHMESH-48z: a gridded face's square rings, O-grid rows and ring-to-box rows are planned this much over the user's
#: shortest edge where they fit so (else at it): planned at it, the smoothing after pulled them a hair under (4.3-4.9 mm
#: under a 5 mm line round a top plate's diamond holes)
EDGE_MARGIN = 1.1
#: a hole's circle: its points within CIRCLE_TOL of its radius from its centre
CIRCLE_TOL = 1e-3
#: SHMESH-51o83: a face mapped by map_faces is not smoothed by gmsh (Mesh.Smoothing 5 applies to transfinite faces
#: too): on a curved o-grid sector round a hole (57.5 mm inner arc, 93.2 mm outer, 29.3 mm deep, 7 round by 3 deep,
#: its sides counted 9.77 mm a segment) the smoothing pulled the rows to 3.3 to 4.7 mm, under the user's 5 mm - 13 of
#: each sector's 21 elements, 515 on the real column (the coordinator's count probe); the look-alike top2 went from
#: 185 elements under 5 mm to none with the smoothing off
MAPPED_UNSMOOTHED = True
#: SHMESH-40: faces with four natural corners meshed as mapped grids
MAPPED = True


@dataclass
class Washer:
    """One planned washer (lengths in model units)."""
    face: int                       # the gmsh surface holding the hole (before the imprint)
    owner: int                      # its sewn face (0-based)
    hole: list[int]                 # the hole's curves (before the imprint)
    centre: np.ndarray
    normal: np.ndarray
    xaxis: np.ndarray               # from the centre to the hole's first point (the ring starts there too)
    r: float
    R: float
    n: int                          # elements round
    rows: int
    out_mm: float                   # the ring's outer element length (mm)
    shrunk: bool = False
    corners: list = field(default_factory=list)  # SHMESH-45f: a square (four-sided) hole's corners, in loop order
    side_counts: list = field(default_factory=list)
    in_box: bool = False            # SHMESH-48h: a square ring drawn in a free box of a gridded face
    in_grid: bool = False           # SHMESH-48k: a square ring made of cells of the grid (no imprint)
    seam: list = field(default_factory=list)  # SHMESH-49e: where its O-grid cuts the hole (the redraw starts it there)
    ring: int = 0                   # after the imprint: the ring's circle
    annulus: int = 0                # ... the surface between hole and ring
    hole_after: list[int] = field(default_factory=list)
    info: dict = field(default_factory=dict)  # SHMESH-45c: the hole's line in the record (quality.structure.hole_list)
    radii: list = field(default_factory=list)  # SHMESH-51: the inner rings' radii over the hole's (else even in ratio)
    plain: tuple = ()               # SHMESH-51: a bolt hole's plan by its size alone (n, rows, ratio), if it falls back
    box: int = 0                    # SHMESH-48q: a free box's cell round its ring (between the ring and the box)
    box_corners: list = field(default_factory=list)   # ... its four corners (model units, round the face's normal)
    owns: bool = False              # SHMESH-51e: its box's sides are its own (no line ends on them)
    kept: bool = False              # SHMESH-51j: one ring inside the two-ring box it keeps (its n, its outer circle)
    square_one: float = 0.0         # SHMESH-51o7: a square bolt hole's ring with two rows: its one-row outer size (R)
    even_under: float = 0.0         # SHMESH-51o75: the user's shortest edge (model units): rows in ratio go even under it

    @property
    def boxed2(self) -> bool:
        """SHMESH-51j: its box is a two-ring bolt box (two rings in it, or one ring in the two-ring box it kept)."""
        return bool(self.radii or self.kept)


def _curve_loops(gmsh, t: int) -> tuple[list, list]:
    """SHMESH-45h: gmsh's curve loops of a surface without the empty ones it gives for some faces (an empty loop read
    as a hole raised in the hole record, or kept a face from its grid)."""
    tags, loops = gmsh.model.occ.getCurveLoops(t)
    keep = [i for i, cl in enumerate(loops) if len(cl)]
    return [tags[i] for i in keep], [loops[i] for i in keep]


def _samples(gmsh, curve: int, n: int = 24) -> np.ndarray:
    lo, hi = gmsh.model.getParametrizationBounds(1, curve)
    ts = np.linspace(lo[0], hi[0], n + 1)
    return np.asarray(gmsh.model.getValue(1, curve, ts.tolist()), dtype=float).reshape(-1, 3)


def _circle_of(gmsh, curves: list[int]) -> tuple[np.ndarray, float, np.ndarray, np.ndarray] | None:
    """(centre, radius, normal, first point) when the loop's curves together are one circle, else None."""
    if not curves:
        return None
    pts = [_samples(gmsh, c) for c in curves]
    allp = np.vstack(pts)
    # the centre: least squares in the loop's own plane
    c0 = allp.mean(axis=0)
    _u, _s, vt = np.linalg.svd(allp - c0, full_matrices=False)
    n = vt[2]
    e1, e2 = vt[0], vt[1]
    xy = np.column_stack([(allp - c0) @ e1, (allp - c0) @ e2])
    A = np.column_stack([2 * xy, np.ones(len(xy))])
    sol, *_r = np.linalg.lstsq(A, (xy ** 2).sum(axis=1), rcond=None)
    centre = c0 + sol[0] * e1 + sol[1] * e2
    rad = np.linalg.norm(allp - centre, axis=1)
    r = float(rad.mean())
    if r <= 0 or float(np.abs(rad - r).max()) > CIRCLE_TOL * r or float(np.abs((allp - centre) @ n).max()) > CIRCLE_TOL * r:
        return None
    # the loop must go once round (the points' angles cover the whole turn)
    ang = np.sort(np.arctan2(xy[:, 1] - sol[1], xy[:, 0] - sol[0]))
    gaps = np.diff(np.concatenate([ang, ang[:1] + 2 * math.pi]))
    if float(gaps.max()) > 0.5 * math.pi:
        return None
    return centre, r, n, pts[0][0]


def _count(r_mm: float, hole_mm: float, face_mm: float, low_mm: float | None = None,
           high_mm: float | None = None) -> tuple[int, int, float] | None:
    """SHMESH-39: (elements round, rows, ring radius over the hole's) for a hole of radius r_mm whose plan puts
    elements of hole_mm round it in a face of face_mm elements: the fewest elements round (a multiple of WASHER_STEP,
    at least WASHER_MIN and the plan's own count) with near-square rows whose outer edge (WASHER_RING times the hole,
    narrower when its elements would come out longer than the face's) has elements no longer than the face's; None
    when no count up to WASHER_MAX does it."""
    own = math.ceil(2 * math.pi * r_mm / max(hole_mm, 1e-12) - 1e-9)
    n0 = max(WASHER_MIN, WASHER_STEP * math.ceil(own / WASHER_STEP))
    for n in range(n0, WASHER_MAX + 1, WASHER_STEP):
        q = math.exp(2 * math.pi / n)
        if low_mm is not None and n > n0 and 2 * math.pi * r_mm / n < low_mm:
            break                      # SHMESH-45e: never an edge round the hole under the user's shortest edge
        for rows in range(1, WASHER_ROWS + 1):
            if q ** rows < WASHER_RING[0]:
                continue
            # as wide as square rows make it, but no wider than the face's size allows round its outer edge and
            # across each row
            ratio = min(q ** rows, WASHER_RING[1], face_mm * n / (2 * math.pi * r_mm), 1 + rows * face_mm / r_mm)
            if high_mm is not None:
                ratio = min(ratio, 0.98 * high_mm * n / (2 * math.pi * r_mm), 1 + rows * 0.98 * high_mm / r_mm)
            if low_mm is not None and (ratio - 1) * r_mm / rows < low_mm:
                # SHMESH-51o25: a row no narrower than the user's shortest edge (EDGE_MARGIN over it where that
                # fits) - the ring wider than the face's size gives - where its outer elements stay under their
                # longest edge and its quads square enough: on the real column 50 one-ring holes of 17.4 mm took rows
                # 4.0 mm wide (the face's size round them) and 400 elements came out under the user's 5 mm
                if RING_AT_LOW:
                    for m_ in (EDGE_MARGIN, 1.0):
                        up = 1 + rows * m_ * low_mm / r_mm
                        if (up <= WASHER_RING[1] and (high_mm is None or 2 * math.pi * r_mm * up / n <= 0.98 * high_mm)
                                and m_ * low_mm <= (0.98 * high_mm if high_mm is not None else math.inf)
                                and 2 * math.pi * r_mm * up / n <= BOLT_ASPECT * m_ * low_mm):
                            return n, rows, up
                if rows < WASHER_ROWS:
                    continue
                break
            if ratio >= WASHER_RING[0]:
                return n, rows, ratio
            break
    return None


#: SHMESH-51: the words for a hole whose two rings the user's edge lengths leave no room for
NO_TWO_EDGES = "the edge lengths asked for leave no two rings round a hole this size"
#: SHMESH-51o52: with a flexible shortest edge, two rings inside its band (LOW_BAND under it), else under it
#: SHMESH-51o56: a face whose rings cut another of its edges tries its two-ring holes at one ring before dropping any
RING_FALLS_TO_ONE = True
RINGS_UNDER_LOW = True   # (SHMESH-51o52, guarded per hole by SHMESH-51o57)
LOW_BAND = 0.2
#: SHMESH-51o75: EVEN ROWS UNDER THE SHORTEST EDGE ON EVERY PATH. A ring of two or more rows is drawn with its rows in
#: ratio (near-square quads) unless a row in ratio comes under the user's shortest edge; then its rows are even across
#: the band (each as wide as the band allows), where every quad stays within BOLT_ASPECT. _count_bolt did so for its
#: own plan, but a hole sent back to its plan by size alone (alike holes kept alike, one_ring, SHMESH-51o56) was drawn
#: in ratio: the real column's 17.42 mm holes with a 10.47 mm band took a 4.22 mm inner row under the user's 5 mm where
#: two even rows of 5.24 mm fit. Where even rows would both come under the user's edge, the rows stay in ratio. The same
#: on the rings planned inside the flexible band (SHMESH-51o52), judged against the user's own edge: the column's
#: 12.24 mm holes (rims of 4.68 mm under its 5 mm) took rows of 4.49 / 7.78 mm where 6.13 each fit
EVEN_UNDER_LOW = True


def _row_radii(r: float, R: float, rows: int, n: int, under: float) -> list[float]:
    """SHMESH-51o75: the radii of a ring's rows from the hole's r to R (any unit): in ratio, or even when a row in
    ratio would be narrower than `under` and even rows are not (and keep every quad within BOLT_ASPECT; else in
    ratio). Where the band cannot hold even rows of `under`, the rows stay in ratio: even, both rows came under it
    (sh0001: 80 ring edges under the 5 mm line went to 144)."""
    ratio = [r * (R / r) ** (j / rows) for j in range(rows + 1)]
    if rows < 2 or under <= 0 or min(b - a for a, b in zip(ratio, ratio[1:], strict=False)) >= under * (1 - 1e-9):
        return ratio
    if (R - r) / rows < under * (1 - 1e-9):
        return ratio
    even = [r + j * (R - r) / rows for j in range(rows + 1)]
    s = math.sin(math.pi / max(n, 3))
    for a, b in zip(even, even[1:], strict=False):
        sides = [2 * a * s, 2 * b * s, b - a]
        if max(sides) > BOLT_ASPECT * min(sides):
            return ratio
    return even


def _count_bolt(r_mm: float, hole_mm: float, face_mm: float, low_mm: float | None = None,
                high_mm: float | None = None, room_mm: tuple[float, float] = (math.inf, math.inf),
                even_mm: float | None = None) -> tuple[int, int, float, list[float]] | str:
    """SHMESH-51: (elements round, rings, outer radius over the hole's, the inner rings' radii over the hole's) of a
    bolt hole's BOLT_RINGS rings: the fewest elements round (a multiple of WASHER_STEP, at least WASHER_MIN and the
    plan's own count) for which the hole's edges stay over the user's shortest edge, each ring is no thinner than it
    (EDGE_MARGIN over it where that fits) nor wider than their longest, the outer elements no longer than their
    longest (BOLT_OUT face sizes when they gave none), every quad within BOLT_ASPECT, the rings out to at most
    BOLT_RING_MAX hole radii and the room (`room_mm`: from the hole's centre to the nearest other edge of its face, and
    its share of the way to the next hole - plan_washers' room check keeps WASHER_ROOM of an outer element clear of an
    edge, half that of a hole). Rings as near square as that allows; the inner ring grown in ratio unless that leaves
    either ring under the shortest edge. Words when none fits."""
    rows = BOLT_RINGS
    e_lo = float(low_mm or 0.0)
    e_hi = 0.98 * float(high_mm) if high_mm is not None else math.inf
    out_hi = e_hi if high_mm is not None else BOLT_OUT * face_mm
    own = math.ceil(2 * math.pi * r_mm / max(hole_mm, 1e-12) - 1e-9)
    n0 = max(WASHER_MIN, WASHER_STEP * math.ceil(own / WASHER_STEP))
    roomy = False
    for n in range(n0, WASHER_MAX + 1, WASHER_STEP):
        s = math.sin(math.pi / n)
        if 2 * r_mm * s < e_lo - 1e-9:
            break                                       # the hole's own edges under the shortest edge
        for margin in ((EDGE_MARGIN, 1.0) if e_lo > 0 and EDGE_MARGIN > 1.0 else (1.0,)):
            w_lo = max(margin * e_lo, 1e-9)
            R_lo = max(WASHER_RING[0] * r_mm, r_mm + rows * w_lo)
            R_hi = min(BOLT_RING_MAX * r_mm, out_hi / (2 * s), r_mm + rows * e_hi)
            if R_lo > R_hi + 1e-9:
                continue
            roomy = True
            a = 2 * math.pi / n
            R_hi = min(R_hi, (1 - 1e-9) * room_mm[0] / (1 + WASHER_ROOM * a),
                       (1 - 1e-9) * room_mm[1] / (1 + 0.5 * WASHER_ROOM * a))
            if R_lo > R_hi + 1e-9:
                continue
            q = math.exp(2 * math.pi / n)
            R = min(max(r_mm * q ** rows, R_lo), R_hi)
            # the rings' radii: in ratio (square rings) unless a ring comes under the shortest edge
            radii = [r_mm * (R / r_mm) ** (j / rows) for j in range(rows + 1)]
            widths = [b - a for a, b in zip(radii, radii[1:], strict=False)]
            if min(widths) < w_lo - 1e-9:
                radii = [r_mm + j * (R - r_mm) / rows for j in range(rows + 1)]
            elif even_mm and EVEN_UNDER_LOW:
                # (SHMESH-51o75: planned inside the flexible band, the rows are still judged against the user's own
                # shortest edge `even_mm` - even where even rows clear it, in ratio where they would not)
                radii = _row_radii(r_mm, R, rows, n, float(even_mm))
            ok = True
            for a, b in zip(radii, radii[1:], strict=False):
                sides = [2 * a * s, 2 * b * s, b - a]
                if max(sides) > BOLT_ASPECT * min(sides) or b - a > e_hi + 1e-9:
                    ok = False
            if ok:
                return n, rows, R / r_mm, [x / r_mm for x in radii[1:-1]]
    if not roomy:
        return NO_TWO_EDGES
    return "no room for two rings"


def _plane_key(n: np.ndarray, c: np.ndarray) -> tuple[np.ndarray, float]:
    """A plane's normal turned one fixed way, and its offset along it."""
    n = n / max(float(np.linalg.norm(n)), 1e-300)
    k = int(np.argmax(np.abs(n)))
    if n[k] < 0:
        n = -n
    return n, float(n @ c)


def _square_of(gmsh, curves: list[int]) -> tuple[np.ndarray, float, np.ndarray] | None:
    """SHMESH-51o7: (centre, inscribed radius, normal) of a four-sided straight-edged hole as the square plan takes
    it (corners 70 to 110 degrees, no side more than twice another), else None."""
    if len(curves) != 4 or any(gmsh.model.getType(1, c) != "Line" for c in curves):
        return None
    ends = [_samples(gmsh, c, 1) for c in curves]
    P = np.array([min(((float(np.linalg.norm(x - y)), x) for x in ends[i] for y in ends[(i + 1) % 4]),
                      key=lambda z: z[0])[1] for i in range(4)])
    n = np.cross(P[1] - P[0], P[2] - P[1])
    if float(np.linalg.norm(n)) <= 0:
        return None
    n = n / float(np.linalg.norm(n))
    sides = [float(np.linalg.norm(P[(i + 1) % 4] - P[i])) for i in range(4)]
    for i in range(4):
        u1, u2 = P[i - 1] - P[i], P[(i + 1) % 4] - P[i]
        ang = math.degrees(math.acos(max(-1.0, min(1.0, float(u1 @ u2) / float(np.linalg.norm(u1) * np.linalg.norm(u2))))))
        if not (70.0 <= ang <= 110.0):
            return None
    if max(sides) > 2.0 * min(sides):
        return None
    return P.mean(axis=0), 0.5 * min(sides), n


def bolt_holes(gmsh, tag_of: dict[int, int], bodies: dict[int, str] | None = None
               ) -> dict[tuple[int, int], tuple[str, str]]:
    """SHMESH-51: which round holes of the flat faces carry load, and why: {(surface, its loop's index): (kind, words)}
    - kind "through" (its axis lines up with a hole of about its size in another face, at most BOLT_REACH diameters
    away: a bolt through both), "pattern" (one of two or more alike holes in one plane of its part: on a circle, in a
    row, or a group), "joined" (its plate meets another plate: an edge it shares with a third face, a flat face out of
    its plane, or another part's face). `bodies`: sewn face -> its part. Holes another face shares are not counted."""
    holes: list[dict] = []
    squares: list[dict] = []        # (SHMESH-51o7: the four-sided holes the square plan takes)
    faces_of: dict[int, set[int]] = {}
    for t in sorted(tag_of):
        if gmsh.model.getType(2, t) != "Plane":
            continue
        _l, loops = _curve_loops(gmsh, t)
        loops = [[abs(int(c)) for c in cl] for cl in loops if len(cl)]
        if len(loops) < 2:
            continue
        length = [sum(float(gmsh.model.occ.getMass(1, c)) for c in cl) for cl in loops]
        outer = int(np.argmax(length))
        for j, cl in enumerate(loops):
            if j == outer or any(len(gmsh.model.getAdjacencies(1, c)[0]) != 1 for c in cl):
                continue
            got = _circle_of(gmsh, cl)
            if got is None:
                sq_ = _square_of(gmsh, cl) if SQUARE_BOLTS else None
                if sq_ is not None:
                    squares.append({"t": t, "j": j, "c": sq_[0], "r": sq_[1], "n": _plane_key(sq_[2], sq_[0])[0]})
                continue
            centre, r, n, _first = got
            nk, off = _plane_key(n, centre)
            holes.append({"t": t, "j": j, "c": centre, "r": r, "n": nk, "off": off,
                          "body": (bodies or {}).get(tag_of[t], "")})
        faces_of[t] = {c for cl in loops for c in cl}
    out: dict[tuple[int, int], tuple[str, str]] = {}
    cos_ax = math.cos(math.radians(BOLT_AXIS_DEG))
    for a in holes:
        for b in holes:
            if b["t"] == a["t"] or abs(float(a["n"] @ b["n"])) < cos_ax:
                continue
            if abs(a["r"] - b["r"]) > BOLT_ALIKE * max(a["r"], b["r"]):
                continue
            v = b["c"] - a["c"]
            along = float(v @ a["n"])
            side = float(np.linalg.norm(v - along * a["n"]))
            if side <= BOLT_AXIS_OFF * min(a["r"], b["r"]) and 1e-9 < abs(along) <= BOLT_REACH * 2 * max(a["r"], b["r"]):
                out[(a["t"], a["j"])] = ("through", "it lines up with a hole in another plate (a bolt through both)")
                break
    for a in holes:
        if (a["t"], a["j"]) in out:
            continue
        alike = [b for b in holes if b is not a and b["body"] == a["body"]
                 and float(a["n"] @ b["n"]) >= cos_ax and abs(b["off"] - a["off"]) <= 1e-3 * max(a["r"], 1e-12)
                 and abs(a["r"] - b["r"]) <= PATTERN_ALIKE * a["r"]]
        if not alike:
            continue
        group = [a, *alike]
        pts = np.array([g["c"] for g in group])
        where = "in a group"
        if len(group) >= 3:
            c0 = pts.mean(axis=0)
            _u, sv, vt = np.linalg.svd(pts - c0, full_matrices=False)
            xy = np.column_stack([(pts - c0) @ vt[0], (pts - c0) @ vt[1]])
            if float(np.abs(xy[:, 1]).max()) <= PATTERN_TOL * a["r"]:
                where = "in a row"
            else:
                A = np.column_stack([2 * xy, np.ones(len(xy))])
                sol, *_r = np.linalg.lstsq(A, (xy ** 2).sum(axis=1), rcond=None)
                rad = np.linalg.norm(xy - sol[:2], axis=1)
                if float(np.abs(rad - rad.mean()).max()) <= PATTERN_TOL * a["r"]:
                    where = "on a circle"
        else:
            where = "in a row"
        out[(a["t"], a["j"])] = ("pattern", f"one of {len(group)} alike holes {where}")
    # a plate joined to another plate: one of its edges shared by three faces or more (a T), with a flat face out of
    # its plane (a welded corner) or with another part's face
    for a in holes:
        if (a["t"], a["j"]) in out:
            continue
        t = a["t"]
        joined = False
        for c in sorted(faces_of.get(t, ())):
            adj = [int(s) for s in gmsh.model.getAdjacencies(1, c)[0] if int(s) != t]
            if not adj:
                continue
            if len(adj) >= 2:
                joined = True
            for s in adj:
                if s in tag_of and (bodies or {}).get(tag_of[s], "") != a["body"]:
                    joined = True
                elif gmsh.model.getType(2, s) == "Plane":
                    lo, hi = gmsh.model.getParametrizationBounds(2, s)
                    ns = np.asarray(gmsh.model.getNormal(s, [0.5 * (lo[0] + hi[0]), 0.5 * (lo[1] + hi[1])]),
                                    dtype=float)[:3]
                    if abs(float(a["n"] @ ns)) / max(float(np.linalg.norm(ns)), 1e-300) < cos_ax:
                        joined = True
            if joined:
                break
        if joined:
            out[(a["t"], a["j"])] = ("joined", "its plate is joined to another plate")
    # SHMESH-51o7: a square hole (a carriage bolt's) lines up with a hole - round or square - of about its size in
    # another plate, or is of a round bolt hole's size on its own plate; else it stays as it was
    for a in squares:
        for b in holes + squares:
            if b is a or b["t"] == a["t"] or abs(float(a["n"] @ b["n"])) < cos_ax:
                continue
            if abs(a["r"] - b["r"]) > BOLT_ALIKE * max(a["r"], b["r"]):
                continue
            v = b["c"] - a["c"]
            along = float(v @ a["n"])
            side = float(np.linalg.norm(v - along * a["n"]))
            if side <= BOLT_AXIS_OFF * min(a["r"], b["r"]) and 1e-9 < abs(along) <= BOLT_REACH * 2 * max(a["r"], b["r"]):
                out[(a["t"], a["j"])] = ("through", "it lines up with a hole in another plate (a bolt through both)")
                break
        if (a["t"], a["j"]) in out:
            continue
        if any((h["t"], h["j"]) in out and tag_of.get(h["t"]) == tag_of.get(a["t"])
               and abs(a["r"] - h["r"]) <= BOLT_ALIKE * max(a["r"], h["r"]) for h in holes):
            out[(a["t"], a["j"])] = ("square", "it is a square hole of bolt size on a plate with bolt holes")
    return out


def _loop_kind(gmsh, curves: list[int]) -> str:
    """SHMESH-45c: what an inner loop that is not one circle is, in a few words: a slot (two straight sides and two
    round ends), an ellipse, a polygon, a circle in pieces of different radius, or its curve types."""
    types = [gmsh.model.getType(1, c) for c in curves]
    lines = sum(1 for x in types if x == "Line")
    arcs = [c for c, x in zip(curves, types, strict=True) if x == "Circle"]
    if any(x == "Ellipse" for x in types):
        return "an ellipse" if lines == 0 else "a loop with elliptic arcs"
    if lines == len(curves):
        return f"a polygon of {lines} straight edges"
    if lines == 2 and len(arcs) == 2:
        return "a slot (two straight sides, two round ends)"
    if arcs and len(arcs) == len(curves):
        radii = []
        for c in arcs:
            got = _circle_of(gmsh, [c]) if False else None
            pts = _samples(gmsh, c, 8)
            mid = pts.mean(axis=0)
            radii.append(float(np.linalg.norm(pts - mid, axis=1).mean()) if got is None else got[1])
        if max(radii) > 1.001 * min(radii):
            return f"a circle in {len(arcs)} pieces of different radius"
        return f"{len(arcs)} arcs that do not make one circle"
    kinds: dict[str, int] = {}
    for x in types:
        kinds[x] = kinds.get(x, 0) + 1
    return "a loop of " + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))


def _frame_points(w: Washer) -> tuple[list[np.ndarray], list[tuple[np.ndarray, np.ndarray]]]:
    """SHMESH-45f: a square washer's lines - (the ring's outer corners Q_i, the 4 short lines that carry each hole side
    on past its far corner out to the ring). The ring is four strips like a pinwheel, each a rectangle along one hole
    side and on past its corner, every corner square: no cell with a 45 degree corner."""
    P = w.corners
    c = w.centre
    width = w.R - w.r
    d = [(P[(i + 1) % 4] - P[i]) / np.linalg.norm(P[(i + 1) % 4] - P[i]) for i in range(4)]
    nrm = []
    for i in range(4):
        n_i = np.cross(d[i], w.normal)
        if float(n_i @ (0.5 * (P[i] + P[(i + 1) % 4]) - c)) < 0:
            n_i = -n_i
        nrm.append(n_i / np.linalg.norm(n_i))
    Q = []
    for i in range(4):
        A = np.array([[float(nrm[i - 1] @ d[i - 1]), float(nrm[i - 1] @ d[i])],
                      [float(nrm[i] @ d[i - 1]), float(nrm[i] @ d[i])]])
        try:
            st = np.linalg.solve(A, np.array([width, width]))
        except np.linalg.LinAlgError:
            st = np.array([0.0, 0.0])
        Q.append(P[i] + st[0] * d[i - 1] + st[1] * d[i])
    ext = []
    for i in range(4):
        j = (i + 1) % 4
        t = width / max(float(d[i] @ nrm[j]), 1e-9)          # side i carried on past P_j to side j's offset
        ext.append((P[j], P[j] + t * d[i]))
    return Q, ext


def _square_plan(gmsh, curves: list[int], face_mm: float, mm: float, hole_size, owner: int, low_mm, high_mm,
                 across: float):
    """SHMESH-45f: a square washer for a four-sided hole of straight edges (a square or diamond hole, a carriage
    bolt's): each side's count from the plan's size round the hole (2 or more), the ring one row of near-square cells
    (as wide as a side's segment, inside the user's edges and the face's size) - four strips along the sides and four
    square corner cells. A Washer (corners set), or why not (words)."""
    if len(curves) != 4 or any(gmsh.model.getType(1, c) != "Line" for c in curves):
        return "not a four-sided hole of straight edges"
    ends = [_samples(gmsh, c, 1) for c in curves]
    corners = []
    for i in range(4):
        a_, b_ = ends[i], ends[(i + 1) % 4]
        corners.append(min(((float(np.linalg.norm(x - y)), x) for x in a_ for y in b_), key=lambda z: z[0])[1])
    P = np.array(corners)
    c = P.mean(axis=0)
    sides = [float(np.linalg.norm(P[(i + 1) % 4] - P[i])) for i in range(4)]
    n = np.cross(P[1] - P[0], P[2] - P[1])
    if float(np.linalg.norm(n)) <= 0:
        return "a degenerate four-sided hole"
    n /= float(np.linalg.norm(n))
    for i in range(4):
        u1, u2 = P[i - 1] - P[i], P[(i + 1) % 4] - P[i]
        ang = math.degrees(math.acos(max(-1.0, min(1.0, float(u1 @ u2) / float(np.linalg.norm(u1) * np.linalg.norm(u2))))))
        if not (70.0 <= ang <= 110.0) or float(np.cross(P[i] - P[i - 1], P[(i + 1) % 4] - P[i]) @ n) <= 0:
            return "a four-sided hole that is not near square (a corner past 70..110 degrees)"
    if max(sides) > 2.0 * min(sides):
        return "a four-sided hole more than twice as long as wide"
    diag = 2 * float(np.linalg.norm(P - c, axis=1).max())
    if diag * mm > across * face_mm:
        return f"larger than the washer limit ({across:g} element sizes across)"
    r_mean = float(np.linalg.norm(P - c, axis=1).mean())
    h_hole = hole_size(r_mean * mm, owner)
    counts = [max(2, round(x * mm / max(h_hole, 1e-9))) for x in sides]
    if low_mm is not None:
        # (SHMESH-48z: no hole side's segment under the user's shortest edge, with the margin where the side allows)
        counts = [max(1, min(m, math.floor(x * mm / (EDGE_MARGIN * float(low_mm)) + 1e-9)
                             or math.floor(x * mm / float(low_mm) + 1e-9))) for x, m in zip(sides, counts, strict=True)]
    seg = float(np.mean([x * mm / m for x, m in zip(sides, counts, strict=True)]))
    width = min(seg, face_mm)
    if high_mm is not None:
        width = min(width, 0.98 * high_mm)
    if low_mm is not None:
        width = max(width, EDGE_MARGIN * float(low_mm))
    w = Washer(face=0, owner=owner, hole=list(curves), centre=c, normal=n,
               xaxis=(P[0] - c) / np.linalg.norm(P[0] - c), r=r_mean, R=r_mean + width / mm, n=sum(counts), rows=1,
               out_mm=max(seg, width))
    w.corners = [np.asarray(x) for x in P]
    w.side_counts = counts
    return w


def _square_presets(gmsh, w: Washer, curves: list[int], preset: dict[int, int], cuts: set[int] | None) -> None:
    """SHMESH-45f: the counts of a square washer's curves after its face is cut: a hole side and the strip's outer
    side its count (plus the corner's row), every strip end the row; the ring's own lines go into `cuts`."""
    Q, ext = _frame_points(w)
    P = w.corners
    tol = 1e-6 * max(1.0, float(w.R))
    width = w.R - w.r
    mids = [0.5 * (P[i] + P[(i + 1) % 4]) for i in range(4)]

    def on_seg(x, p0, p1) -> bool:
        d_ = p1 - p0
        tt = float((x - p0) @ d_) / max(float(d_ @ d_), 1e-300)
        return -1e-9 <= tt <= 1 + 1e-9 and float(np.linalg.norm(p0 + tt * d_ - x)) <= tol

    for c in curves:
        a_, b_ = _samples(gmsh, c, 1)
        ln = float(np.linalg.norm(b_ - a_))
        hole_side = next((i for i in range(4) if on_seg(a_, P[i], P[(i + 1) % 4])
                          and on_seg(b_, P[i], P[(i + 1) % 4])), None)
        if hole_side is not None:
            preset[c] = w.side_counts[hole_side]
            continue
        ring = any(on_seg(a_, Q[i], Q[(i + 1) % 4]) and on_seg(b_, Q[i], Q[(i + 1) % 4]) for i in range(4))
        short = any(on_seg(a_, p0, p1) and on_seg(b_, p0, p1) for p0, p1 in ext)
        if not (ring or short):
            continue
        if short or abs(ln - width) <= 0.1 * width:
            preset[c] = w.rows
        else:
            near = int(np.argmin([float(np.linalg.norm(0.5 * (a_ + b_) - m)) for m in mids]))
            preset[c] = w.side_counts[near] + w.rows
        if cuts is not None:
            cuts.add(c)


#: SHMESH-51j: a bolt hole sent to one ring for a fault in its rings keeps its two-ring box (place, size, count) and
#: takes one row out to its two rings' outer circle, so the band and every line of its face stay as they were
KEEP_BOX = True


def _one_row_fits(r_mm: float, n: int, R_mm: float, low_mm, high_mm, aims=()) -> bool:
    """SHMESH-51j: whether one row of n quads from a hole of radius r_mm out to the circle R_mm is inside the solver's
    limits, the user's quad lines (else BAND_AIMS) and the user's shortest and longest edge (one quad: the row's
    quads are all alike)."""
    from meshpipeline.engines import shell_quality as SQ

    a = 2 * math.pi / n
    xyz = np.array([[r_mm, 0.0, 0.0], [R_mm, 0.0, 0.0], [R_mm * math.cos(a), R_mm * math.sin(a), 0.0],
                    [r_mm * math.cos(a), r_mm * math.sin(a), 0.0]])
    quads = np.array([[0, 1, 2, 3]])
    meas = SQ.quad_measures(xyz, quads, extra=True)
    if SQ._quad_past(meas).any():
        return False
    if (SQ.aim_score(meas, "quad", SQ.InForce(aims=tuple(aims) if aims else BAND_AIMS), 1) > 0).any():
        return False
    ed = np.linalg.norm(xyz - np.roll(xyz, -1, axis=0), axis=1)
    if high_mm is not None and float(ed.max()) > 0.98 * float(high_mm):
        return False
    return not (low_mm is not None and float(ed.min()) < float(low_mm))


def plan_washers(gmsh, tag_of: dict[int, int], size_mm: dict[int, float], hole_size, mm: float,
                 names: dict[int, tuple[str, str]] | None = None, before: list | None = None,
                 across: float | None = None, low_mm: float | None = None, high_mm: float | None = None,
                 no_bolt: dict | None = None, one_ring_at: list | None = None, quad_aims: tuple = (),
                 low_soft: bool = False, low_soft_off: set[int] | frozenset = frozenset()
                 ) -> tuple[list[Washer], dict[str, int], list[dict]]:
    """SHMESH-39: the washers to make: every inner loop of a flat face that is one round hole (a circle no other face
    shares), at most WASHER_ACROSS of the face's element size across. `size_mm`: each surface's element size;
    `hole_size(r_mm, owner)`: the element length the plan puts round a hole of that radius. Returns (the washers,
    {why a hole has none: count}, SHMESH-45c: one line per inner loop of a flat face - its face and part (`names`:
    owner -> (mid-face, part)), diameter, CAD edges before (`before`: [(centre, edges)] from the SHMESH-45b redraw)
    and after, whether another face shares its loop, the ring's planned count and width, the room to the nearest face
    edge and other hole (element sizes), and why it has no washer; the washers' lines are filled in further on)."""
    skipped: dict[str, int] = {}
    found: list[Washer] = []
    lines: list[dict] = []
    # SHMESH-51: the holes that carry load (bolt holes), and why
    bolts = bolt_holes(gmsh, tag_of, {k: b for k, (_m, b) in (names or {}).items()}) if BOLTS else {}

    def no(info: dict, reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1
        info["washer"] = False
        info["why"] = info.get("why") or reason

    for t, k in sorted(tag_of.items()):
        if gmsh.model.getType(2, t) != "Plane":
            continue
        _l, loops = _curve_loops(gmsh, t)
        # (an empty loop - gmsh gives one for some faces - is no hole: the record's sampling of it raised and cost
        # every washer of the part, SHMESH-45h)
        loops = [[abs(int(c)) for c in cl] for cl in loops if len(cl)]
        if len(loops) < 2:
            continue
        # the outer loop is the longest
        length = [sum(float(gmsh.model.occ.getMass(1, c)) for c in cl) for cl in loops]
        outer = int(np.argmax(length))
        h = size_mm[t]
        rims = [_samples(gmsh, c, 64) for c in loops[outer]]
        here = {}
        for j, cl in enumerate(loops):
            if j != outer:
                pts = np.vstack([_samples(gmsh, c, 16) for c in cl])
                cj = pts.mean(axis=0)
                here[j] = (cj, float(np.linalg.norm(pts - cj, axis=1).mean()))
        for j, cl in enumerate(loops):
            if j == outer:
                continue
            mid, part = (names or {}).get(k, (str(k), ""))
            c0, r0 = here[j]
            edge_gap = min(float(np.linalg.norm(p - c0, axis=1).min()) for p in rims) - r0
            hole_gap = min((float(np.linalg.norm(c - c0)) - r - r0 for jj, (c, r) in here.items() if jj != j),
                           default=math.inf)
            n_before = next((int(e) for cc, e in (before or [])
                             if float(np.linalg.norm(np.asarray(cc, dtype=float) - c0)) <= 0.25 * max(r0, 1e-9)),
                            len(cl))
            info = {"face": mid, "part": part, "diameter_mm": float(f"{2 * r0 * mm:.4g}"),
                    "at_mm": [float(f"{x * mm:.6g}") for x in c0],
                    "edges_before": n_before, "edges": len(cl),
                    "shared": any(len(gmsh.model.getAdjacencies(1, c)[0]) != 1 for c in cl),
                    "to_edge": float(f"{edge_gap * mm / h:.3g}"),
                    "to_hole": float(f"{hole_gap * mm / h:.3g}") if math.isfinite(hole_gap) else None}
            lines.append(info)
            if info["shared"]:
                no(info, "shared with another face")
                continue
            got = _circle_of(gmsh, cl)
            if got is None:
                info["loop"] = _loop_kind(gmsh, cl)
                sq = _square_plan(gmsh, cl, size_mm[t], mm, hole_size, k, low_mm, high_mm,
                                  across if across is not None else WASHER_ACROSS)
                if isinstance(sq, str):
                    no(info, f"not round: {sq}")
                    continue
                bolt_sq = bolts.get((t, j)) if SQUARE_BOLTS else None
                if bolt_sq is not None:
                    # SHMESH-51o7: a square bolt hole - a second row of near-square cells round its square ring (each
                    # row as wide as its one row, inside the user's edges), where it fits (room checked below)
                    info["bolt"] = bolt_sq[1]
                    if k in (no_bolt or {}):
                        info["one_ring"] = (no_bolt or {})[k]
                    else:
                        sq.square_one = sq.R
                        sq.R, sq.rows = sq.r + 2 * (sq.R - sq.r), 2
                info.update(planned_round=sum(sq.side_counts), planned_rows=sq.rows,
                            planned_width_mm=float(f"{(sq.R / sq.r - 1) * sq.r * mm:.4g}"), shape="square")
                sq.face, sq.owner, sq.hole, sq.info = t, k, cl, info
                found.append(sq)
                continue
            centre, r, n, first = got
            limit = across if across is not None else WASHER_ACROSS
            if 2 * r * mm > limit * size_mm[t]:
                no(info, f"larger than the washer limit ({limit:g} element sizes across)")
                continue
            # SHMESH-45e: inside the user's shortest and longest edge where a count fits, else as before
            plan = (_count(r * mm, hole_size(r * mm, k), size_mm[t], low_mm, high_mm)
                    or _count(r * mm, hole_size(r * mm, k), size_mm[t]))
            if plan is None:
                no(info, "no count fits")
                continue
            cnt, rows, ratio = plan
            plain = (max(cnt, WASHER_STEP * math.ceil(len(cl) / WASHER_STEP)), rows, ratio)
            radii: list[float] = []
            kept = False
            bolt = bolts.get((t, j))
            if bolt is not None:
                # SHMESH-51: a bolt hole - BOLT_RINGS rings where they fit (its count chosen for them), else the plan
                info["bolt"] = bolt[1]
                gap_mm, share_mm, where = _room_of(gmsh, t, cl, centre, r, [x for jj, x in here.items() if jj != j],
                                                   mm)
                got_b = (_count_bolt(r * mm, hole_size(r * mm, k), size_mm[t], low_mm, high_mm,
                                     room_mm=(gap_mm, share_mm)) if k not in (no_bolt or {})
                         else (no_bolt or {})[k])
                if got_b == NO_TWO_EDGES and low_soft and low_mm and RINGS_UNDER_LOW and k not in low_soft_off:
                    # SHMESH-51o52: with the user's shortest edge flexible, two rings come before it (the owner's
                    # order: rings before edge length) - first inside its band (LOW_BAND under it), then under it,
                    # where the rings still keep every quad within BOLT_ASPECT; their shape lines and the solver's
                    # limits are checked on the mesh as for any ring (on the real column 8 bolt holes of 12.24 mm in
                    # a row kept one ring: the 5 mm shortest edge left no two rings round a hole that size)
                    for low_try, where_ in ((float(low_mm) * (1 - LOW_BAND), "inside the flexible band"),
                                            (None, "under the band (flexible: rings before edge length)")):
                        got_s = _count_bolt(r * mm, hole_size(r * mm, k), size_mm[t], low_try, high_mm,
                                            room_mm=(gap_mm, share_mm), even_mm=low_mm)
                        if isinstance(got_s, tuple):
                            rad_ = [1.0, *got_s[3], got_s[2]]
                            w_min = min(b - a for a, b in zip(rad_, rad_[1:], strict=False)) * r * mm
                            info["rings_under_shortest_edge"] = (
                                f"two rings with rows {w_min:.2f} mm wide, under the shortest edge "
                                f"{float(low_mm):g} mm, {where_}")
                            got_b = got_s
                            break
                        if got_s != NO_TWO_EDGES:
                            break
                sent = [c_ for c_ in one_ring_at or []
                        if float(np.linalg.norm(np.asarray(c_[:3], dtype=float) - centre)) <= 0.25 * r]
                if sent and KEEP_BOX and isinstance(got_b, tuple) and got_b[0] >= len(cl) and all(
                        len(c_) > 3 for c_ in sent) and _one_row_fits(r * mm, got_b[0], got_b[2] * r * mm, low_mm,
                                                                       high_mm, quad_aims):
                    # SHMESH-51j: a fault one ring cures - one row out to its two-ring outer circle, in the box its two
                    # rings had (same place, size and count): no line of its face moves, its neighbours stay as they are
                    cnt, rows, ratio, radii, kept = got_b[0], 1, got_b[2], [], True
                    info["one_ring"] = "one ring in its two-ring box (a fault in its rings)"
                    got_b = None
                elif sent and info.pop("rings_under_shortest_edge", None):
                    # (SHMESH-51o57/58: its rings under the shortest edge were tried and sent back on an earlier pass)
                    got_b = "its rings under the shortest edge left a line worse than one ring"
                elif sent:
                    # (SHMESH-51d: its two-ring box failed in its face's grid on an earlier pass)
                    got_b = "one ring so its face keeps its grid"
                if got_b is None:
                    pass
                elif isinstance(got_b, tuple) and got_b[0] >= len(cl):
                    cnt, rows, ratio, radii = got_b
                else:
                    info["one_ring"] = (got_b if isinstance(got_b, str) else "a hole drawn in many pieces") + (
                        f" ({where})" if isinstance(got_b, str) and got_b.startswith("no room") else "")
            # a hole drawn as many short curves (a faceted circle) carries at least one element on each
            cnt = max(cnt, WASHER_STEP * math.ceil(len(cl) / WASHER_STEP))
            xa = first - centre
            xa = xa - float(xa @ n) * n
            xa /= max(float(np.linalg.norm(xa)), 1e-300)
            info.update(planned_round=cnt, planned_rows=rows, planned_width_mm=float(f"{(ratio - 1) * r * mm:.4g}"))
            found.append(Washer(face=t, owner=k, hole=cl, centre=centre, normal=n, xaxis=xa, r=r, R=ratio * r,
                                n=cnt, rows=rows, out_mm=2 * math.pi * ratio * r * mm / cnt, info=info,
                                radii=radii, plain=plain if (radii or kept) else (), kept=kept,
                                even_under=float(low_mm) / mm if (low_mm and EVEN_UNDER_LOW) else 0.0))
    # SHMESH-51: alike holes of one face take one plan - two rings only when every one of them has room for two (a row
    # of bolt holes with one roomier hole gave that one a bigger box than its neighbours', and the grid broke)
    for w in found:
        if not w.radii:
            continue
        kin = [o for o in found if o.face == w.face and not o.corners and abs(o.r - w.r) <= PATTERN_ALIKE * w.r]
        # (a hole held to one ring so its face keeps its grid does not hold its alike holes to one)
        # (SHMESH-51o58: nor one sent back from rings under the shortest edge - each such hole is judged alone)
        if any(not o.radii and not o.kept and not str(o.info.get("one_ring", "")).startswith(
                ("one ring so", "its rings under the shortest edge"))
               for o in kin):
            n_, rows_, ratio_ = w.plain
            w.n, w.rows, w.R, w.radii = n_, rows_, ratio_ * w.r, []
            w.out_mm = 2 * math.pi * w.R * mm / w.n
            w.info.update(planned_round=n_, planned_rows=rows_, planned_width_mm=float(f"{(ratio_ - 1) * w.r * mm:.4g}"),
                          one_ring="no room for two rings round every alike hole beside it (kept alike)")
    # room: each ring against the other edges of its face and the other holes' rings
    keep: list[Washer] = []
    for w in found:
        others = [c for c in {abs(int(c)) for _d, c in gmsh.model.getBoundary([(2, w.face)], oriented=False)}
                  if c not in w.hole]
        gap = math.inf
        for c in sorted(others):
            p = _samples(gmsh, c, 64)
            gap = min(gap, float(np.linalg.norm(p - w.centre, axis=1).min()))
        limit = gap - WASHER_ROOM * w.out_mm / mm
        for o in found:
            if o is w or o.face != w.face:
                continue
            d = float(np.linalg.norm(o.centre - w.centre))
            limit = min(limit, d * w.r / (w.r + o.r) - 0.5 * WASHER_ROOM * w.out_mm / mm)
        if (w.radii or w.kept) and w.plain:
            # (SHMESH-51: the one-ring plan it falls back to, room-checked as any washer is - kept for one_ring)
            n_p, rows_p, ratio_p = w.plain
            out_p = 2 * math.pi * ratio_p * w.r * mm / n_p
            lim_p = gap - WASHER_ROOM * out_p / mm
            for o in found:
                if o is not w and o.face == w.face:
                    lim_p = min(lim_p, float(np.linalg.norm(o.centre - w.centre)) * w.r / (w.r + o.r)
                                - 0.5 * WASHER_ROOM * out_p / mm)
            if lim_p < ratio_p * w.r:
                if lim_p < WASHER_SHRUNK * w.r:
                    w.plain = ()
                else:
                    R_p = lim_p
                    w.plain = (n_p, max(1, min(rows_p, round(math.log(R_p / w.r) / (2 * math.pi / n_p)))),
                               R_p / w.r, True)
        if w.corners and w.rows == 2 and w.square_one and limit < w.R:
            # (SHMESH-51o7: no room for a square bolt hole's second row: its one row)
            one_ring(w, mm, "no room for two rings")
        if limit < w.R and not ((w.radii or w.kept) and limit >= w.R * (1 - 1e-6)):
            if (w.radii or w.kept) and one_ring(w, mm, "no room for two rings (narrowed by the holes beside it)"):
                # (SHMESH-51: narrowed by its neighbours' rings - the one ring its size gives, room-checked)
                w.info["washer"] = True
                keep.append(w)
                continue
            if limit < WASHER_SHRUNK * w.r:
                no(w.info, "too close to an edge or another hole")
                continue
            w.R, w.shrunk = limit, True
            w.rows = max(1, min(w.rows, round(math.log(w.R / w.r) / (2 * math.pi / w.n))))
            w.out_mm = 2 * math.pi * w.R * mm / w.n
            w.info["shrunk_width_mm"] = float(f"{(w.R - w.r) * mm:.4g}")
            if w.radii:
                # (SHMESH-51: a bolt hole's rings narrowed by its neighbours' - one ring)
                w.radii, w.rows = [], 1
                w.info["one_ring"] = "no room for two rings (narrowed by the holes beside it)"
        w.info["washer"] = True
        keep.append(w)
    return keep, skipped, lines


def one_ring(w: Washer, mm: float, why: str) -> bool:
    """SHMESH-51: a bolt hole's washer back to the one ring its size gives (its plan by size alone, room-checked), the
    reason on its line; False when it has no such plan (it keeps its two rings)."""
    if w.corners and w.rows == 2 and w.square_one:
        # (SHMESH-51o7: a square bolt hole's ring back to its one row of near-square cells)
        w.rows, w.R, w.square_one = 1, w.square_one, 0.0
        w.info.update(planned_rows=1, planned_width_mm=float(f"{(w.R - w.r) * mm:.4g}"), one_ring=why)
        return True
    if not (w.radii or w.kept) or not w.plain:
        return False
    n_, rows_, ratio_, *shrunk = w.plain
    w.n, w.rows, w.R, w.radii, w.kept = n_, rows_, ratio_ * w.r, [], False
    w.out_mm = 2 * math.pi * w.R * mm / w.n
    w.shrunk = bool(shrunk and shrunk[0])
    w.info.update(planned_round=n_, planned_rows=rows_, planned_width_mm=float(f"{(ratio_ - 1) * w.r * mm:.4g}"),
                  one_ring=why)
    if w.shrunk:
        w.info["shrunk_width_mm"] = float(f"{(w.R - w.r) * mm:.4g}")
    return True


def _room_of(gmsh, t: int, hole: list[int], centre: np.ndarray, r: float, others: list[tuple[np.ndarray, float]],
             mm: float) -> tuple[float, float, str]:
    """SHMESH-51: the room round a hole (mm from its centre): to the nearest other edge of its face, and the share of
    the way to each other hole of the face that is its own (by radius), with the nearest thing in words."""
    gap, near = math.inf, ""
    for c in sorted({abs(int(c)) for _d, c in gmsh.model.getBoundary([(2, t)], oriented=False)} - set(hole)):
        d = float(np.linalg.norm(_samples(gmsh, c, 64) - centre, axis=1).min())
        if d < gap:
            on_hole = any(abs(float(np.linalg.norm(oc - centre)) - orr - d) <= 0.05 * max(orr, 1e-12) and
                          float(np.linalg.norm(_samples(gmsh, c, 4) - oc, axis=1).max()) <= 1.05 * orr
                          for oc, orr in others)
            shared = len(gmsh.model.getAdjacencies(1, c)[0]) > 1
            gap, near = d, ("the next hole" if on_hole else "a joint with another face" if shared else
                            "the plate's edge")
    share = min((float(np.linalg.norm(oc - centre)) * r / (r + orr) for oc, orr in others), default=math.inf)
    # (the nearer bound, as _count_bolt takes them: the edge with half an outer element kept clear, a hole a quarter)
    if math.isfinite(share) and share * 1.16 < gap:
        gap_h = min(float(np.linalg.norm(oc - centre)) - orr for oc, orr in others)
        where = f"the next hole is {(gap_h - r) * mm:.3g} mm away"
    else:
        where = f"{near} is {(gap - r) * mm:.3g} mm away" if math.isfinite(gap) else "no room"
    return gap * mm, share * mm, where


def _on_hole(gmsh, w: Washer, curves: list[int]) -> bool:
    """Whether these curves lie on the washer's hole circle (the annulus's other loop; the face outside a ring has a
    loop of two too when the ring's hole is its only one)."""
    return all(bool(np.all(np.abs(np.linalg.norm(_samples(gmsh, c, 4) - w.centre, axis=1) - w.r) <= CIRCLE_TOL * w.r))
               for c in curves)


#: SHMESH-51q: a gridded face's planning does not count a CAD piece of a round hole's own circle as a cell edge
HOLE_PIECES = True


def _on_any_hole(gmsh, c: int, ws: list[Washer]) -> bool:
    """SHMESH-51q: whether a curve is a piece of one of these washers' round hole circles."""
    p = _samples(gmsh, c, 4)
    return any(not w.corners and bool(np.all(np.abs(np.linalg.norm(p - w.centre, axis=1) - w.r) <= CIRCLE_TOL * w.r))
               for w in ws)


def _on_square(gmsh, w: Washer, curves: list[int]) -> bool:
    """SHMESH-48h: whether these curves (a loop) lie on a square washer's hole sides and go all round it."""
    if not curves or not w.corners:
        return False
    P = w.corners
    tol = 1e-4 * max(float(w.r), 1e-12)
    total = 0.0
    for c in curves:
        q = _samples(gmsh, c, 4)
        dist = np.full(len(q), np.inf)
        for i in range(4):
            a_, d_ = P[i], P[(i + 1) % 4] - P[i]
            tt = np.clip(((q - a_) @ d_) / max(float(d_ @ d_), 1e-300), 0.0, 1.0)
            dist = np.minimum(dist, np.linalg.norm(a_ + tt[:, None] * d_ - q, axis=1))
        if float(dist.max()) > tol:
            return False
        total += float(gmsh.model.occ.getMass(1, c))
    rim = sum(float(np.linalg.norm(P[(i + 1) % 4] - P[i])) for i in range(4))
    return abs(total - rim) <= 1e-3 * rim


def _rings_whole(gmsh, t: int, ws: list[Washer], omap) -> bool:
    """Whether a fragment of face t by the washers' circles left each ring one circle closing round its hole."""
    new = [s for d, s in omap[0] if d == 2]
    for got in omap[1:len(ws) + 1]:
        curves = [c for d, c in got if d == 1]
        if len(curves) != 1:
            return False
        if not any([curves[0]] in [[abs(int(c)) for c in cl] for cl in _curve_loops(gmsh, s)[1]]
                   and len(_curve_loops(gmsh, s)[1]) == 2 for s in new):
            return False
    return True


def imprint(gmsh, washers: list[Washer], tag_of: dict[int, int], cuts: set[int] | None = None,
            preset: dict[int, int] | None = None, square_cells: set[int] | None = None) -> list[Washer]:
    """SHMESH-39/45f: each face's washers drawn on it in ONE cut - a round hole's ring (a circle about its centre,
    starting where the hole's circle starts), a square hole's four strips - so no washer holds a tag the cut has
    replaced (two cuts of one face, one for its rings and one for its square rings, left the second with a stale face:
    "Unknown OpenCASCADE entity of dimension 2"). `tag_of` follows; edges the face shares keep their tags, so every
    junction stays conformal. Tried on a copy first: a face where a ring would come out cut keeps no washer of that
    kind (rings, then square rings, tried alone). SHMESH-45g: a face that fails in any way after that keeps the washers
    drawn so far and loses only its own (logged, and the record says why). A square ring's counts go into `preset`,
    its lines into `cuts`. Returns the washers drawn."""
    by_face: dict[int, list[Washer]] = {}
    for w in washers:
        by_face.setdefault(w.face, []).append(w)
    kept: list[Washer] = []
    occ = gmsh.model.occ
    if preset is None:
        preset = {}

    def tools(ws: list[Washer]) -> list[tuple[int, int]]:
        out = [(1, occ.addCircle(*[float(x) for x in w.centre], float(w.R), zAxis=[float(x) for x in w.normal],
                                 xAxis=[float(x) for x in w.xaxis])) for w in ws if not w.corners]
        for w in ws:
            if w.corners:
                Q, ext = _frame_points(w)
                qp = [occ.addPoint(*[float(x) for x in q]) for q in Q]
                for i in range(4):
                    out.append((1, occ.addLine(qp[i], qp[(i + 1) % 4])))
                for p0, p1 in ext:
                    out.append((1, occ.addLine(occ.addPoint(*[float(x) for x in p0]),
                                               occ.addPoint(*[float(x) for x in p1]))))
        return out

    def trial(t: int, ws: list[Washer]) -> bool:
        rounds = [w for w in ws if not w.corners]
        copy = occ.copy([(2, t)])
        _o, cmap = occ.fragment(copy, tools(ws))
        occ.synchronize()
        cells = [x for d, x in cmap[0] if d == 2]
        ok = len(cells) == 1 + len(rounds) + 4 * (len(ws) - len(rounds)) and (
            not rounds or _rings_whole(gmsh, t, rounds, cmap))
        if FAST_REMOVE:
            _remove_cells(gmsh, cells)
        else:
            occ.remove([(2, x) for x in cells], recursive=True)
        occ.synchronize()
        _remove_loose(gmsh, {c for got in cmap[1:] for d, c in got if d == 1})
        return ok

    for t, ws in sorted(by_face.items()):
        if t not in tag_of:
            for w in ws:
                w.info.update(washer=False, why="its face was replaced before its ring was drawn")
            continue
        try:
            use = ws
            two = [w for w in ws if w.radii and w.plain and not w.box_corners]   # (a box was planned round its R)
            if two and RING_FALLS_TO_ONE and not trial(t, use):
                # SHMESH-51o56: two rings that would cut another edge of the face fall back to one ring first, never
                # to none (on fr011, with the held holes let back to two rings, 8 of 24 holes lost their washers)
                for w in two:
                    n_, rows_, ratio_ = w.plain
                    mm_w = w.out_mm * w.n / max(2 * math.pi * w.R, 1e-300)     # (mm per model unit)
                    w.n, w.rows, w.R, w.radii = n_, rows_, ratio_ * w.r, []
                    w.out_mm = 2 * math.pi * w.R * mm_w / max(w.n, 1)
                    w.info["one_ring"] = "its second ring would cut another edge of its face"
                    w.info.update(planned_round=n_, planned_rows=rows_,
                                  planned_width_mm=float(f"{(ratio_ - 1) * w.r * mm_w:.4g}"))
            if not trial(t, use):
                rounds = [w for w in ws if not w.corners]
                squares = [w for w in ws if w.corners]
                use = rounds if rounds and trial(t, rounds) else (squares if squares and trial(t, squares) else [])
            for w in ws:
                if w not in use:
                    w.info.update(washer=False, why="its ring would cut another edge of its face" if not w.corners
                                  else "its square ring would cut another edge of its face")
            if not use:
                continue
            _out, omap = occ.fragment([(2, t)], tools(use))
            occ.synchronize()
            k = tag_of.pop(t)
            new = [x for d, x in omap[0] if d == 2]
            for x in new:
                tag_of[x] = k
            _remove_loose(gmsh, {c for got in omap[1 + sum(1 for w in use if not w.corners):]
                                 for d, c in got if d == 1})
            if cuts is not None and any(w.in_box for w in use):
                # SHMESH-48h: a box cell's edges that no other face shares come back with new tags from the cut
                cuts.intersection_update(c for _d, c in gmsh.model.getEntities(1))
            curves = sorted({abs(int(c)) for x in new for _d, c in gmsh.model.getBoundary([(2, x)], oriented=False)})
            for w, got in zip([w for w in use if not w.corners], omap[1:], strict=False):
                w.ring = [c for d, c in got if d == 1][0]
                for x in new:
                    _l, loops = _curve_loops(gmsh, x)
                    loops = [[abs(int(c)) for c in cl] for cl in loops]
                    if len(loops) == 2 and [w.ring] in loops:
                        other = [cl for cl in loops if cl != [w.ring]][0]
                        if _on_hole(gmsh, w, other):
                            w.annulus = x
                            w.hole_after = other
                if w.annulus:
                    kept.append(w)
                else:
                    w.info.update(washer=False, why="its ring did not close round its hole")
            if square_cells is not None and any(w.corners for w in use):
                square_cells.update(new)
            for w in use:
                if w.corners:
                    _square_presets(gmsh, w, curves, preset, cuts)
                    w.info["kind"] = "square ring in a free box of a gridded face" if w.in_box \
                        else "square ring in a free face"
                    kept.append(w)
        except Exception as exc:  # noqa: BLE001 - SHMESH-45g: one face's washers never cost the others
            import logging

            logging.getLogger(__name__).info("shell mesh: the washers of one face were not drawn (%s)", exc)
            for w in ws:
                if w not in kept:
                    w.info.update(washer=False, why=f"its face's washers failed ({type(exc).__name__}: {exc})"[:300])
    return kept


def set_counts(gmsh, washers: list[Washer]) -> set[int]:
    """SHMESH-39: the hole's circle and the ring's carry the washer's count, evenly spaced; returns those curves."""
    fixed: set[int] = set()
    for w in washers:
        lengths = [float(gmsh.model.occ.getMass(1, c)) for c in w.hole_after]
        counts = _spread(w.n, lengths)
        if counts is None:
            raise ValueError(f"a washer's hole has {len(lengths)} curves for {w.n} elements round it")
        for c, m in zip(w.hole_after, counts, strict=True):
            gmsh.model.mesh.setTransfiniteCurve(c, m + 1)
            fixed.add(c)
        gmsh.model.mesh.setTransfiniteCurve(w.ring, w.n + 1)
        fixed.add(w.ring)
    return fixed


def _loop_nodes(gmsh, curves: list[int], index: np.ndarray) -> np.ndarray:
    """The mesh nodes (0-based, the adapter's index) on these curves, each once."""
    out: list[int] = []
    for c in curves:
        tags, _x, _p = gmsh.model.mesh.getNodes(1, c, includeBoundary=True)
        out += index[np.asarray(tags, dtype=np.int64)].tolist()
    return np.array(sorted(set(out)), dtype=np.int64)


#: SHMESH-45d: the ring's nodes slide to their hole nodes' angles when they stand at most this share of an element off
#: them (more: the ring is refused and gmsh's mesh kept, SHMESH-45)
RING_FOLLOW = 0.75


def ring_mesh(gmsh, w: Washer, index: np.ndarray, xyz: np.ndarray, mm: float) -> tuple[np.ndarray, np.ndarray]:
    """SHMESH-39: the washer's own structured mesh, in place of whatever gmsh put between hole and ring: each node of the
    hole's circle joined to the ring's node at its angle, the rows between them on circles whose radii grow evenly
    (in ratio), so every quad is near square. The ring's own nodes are slid along the ring to their hole nodes' angles
    (in `xyz`, in place) when the hole's are not evenly spaced (SHMESH-45d). Returns (the new nodes, mm; the quads,
    node indices - new nodes numbered from len(xyz))."""
    inner = _loop_nodes(gmsh, w.hole_after, index)
    outer = _loop_nodes(gmsh, [w.ring], index)
    if len(inner) != w.n or len(outer) != w.n:
        raise ValueError(f"a washer's circles carry {len(inner)} and {len(outer)} nodes, not {w.n}")
    c = w.centre * mm
    e1 = w.xaxis
    e2 = np.cross(w.normal, e1)

    def angles(nodes: np.ndarray) -> np.ndarray:
        rel = xyz[nodes] - c
        return np.mod(np.arctan2(rel @ e2, rel @ e1), 2 * math.pi)

    a_in, a_out = angles(inner), angles(outer)
    inner, a_in = inner[np.argsort(a_in)], np.sort(a_in)
    outer, a_out = outer[np.argsort(a_out)], np.sort(a_out)
    step = 2 * math.pi / w.n
    diff = (a_out[None, :] - a_in[0] + math.pi) % (2 * math.pi) - math.pi
    shift = int(np.argmin(np.abs(diff[0])))
    outer, a_out = np.roll(outer, -shift), np.roll(a_out, -shift)
    gap = (a_out - a_in + math.pi) % (2 * math.pi) - math.pi
    if float(np.abs(gap).max()) > RING_FOLLOW * step:
        raise ValueError(f"a washer's hole and ring nodes do not face each other ({float(np.abs(gap).max()) / step:.2f} "
                         "of an element apart)")
    if float(np.abs(gap).max()) > 1e-9 * step:
        # SHMESH-45d: a hole whose nodes are not evenly spaced (its circle in uneven pieces, or pieces not exactly one
        # circle) - each ring node is slid along the ring to its hole node's angle, so every spoke is radial
        Rm = w.R * mm
        xyz[outer] = c + Rm * (np.cos(a_in)[:, None] * e1 + np.sin(a_in)[:, None] * e2)
        w.info["followed"] = float(f"{float(np.abs(gap).max()) / step:.3g}")      # SHMESH-45k: drawn again next pass
        gap = np.zeros_like(gap)
    rings = [inner]
    new: list[np.ndarray] = []
    base = len(xyz)
    # (SHMESH-51o75: with no radii of its own, rows in ratio unless one comes under the user's shortest edge)
    drawn = ([w.r, *[x * w.r for x in w.radii], w.R] if len(w.radii) == w.rows - 1
             else _row_radii(w.r, w.R, w.rows, w.n, w.even_under if EVEN_UNDER_LOW else 0.0))
    if w.rows >= 2:
        w.info["rows_mm"] = [float(f"{(b - a) * mm:.4g}") for a, b in zip(drawn, drawn[1:], strict=False)]
    for j in range(1, w.rows):
        f = j / w.rows
        # (SHMESH-51: a bolt hole's rings at their own radii, so neither comes under the user's shortest edge)
        rad = drawn[j] * mm
        ang = a_in + f * gap
        pts = c + rad * (np.cos(ang)[:, None] * e1 + np.sin(ang)[:, None] * e2)
        ids = np.arange(base, base + w.n)
        base += w.n
        new.append(pts)
        rings.append(ids)
    rings.append(outer)
    quads = []
    for a, b in zip(rings[:-1], rings[1:], strict=True):
        quads.append(np.column_stack([a, np.roll(a, -1), np.roll(b, -1), b]))
    return (np.vstack(new) if new else np.zeros((0, 3))), np.vstack(quads)


#: SHMESH-40: a face is mapped when its one loop has four CORNERS (where two curves meet between MAP_CORNER degrees;
#: where they run on within SMOOTH_DEG of straight it is no corner, so a side may be a chain of curves) and its
#: opposite sides are alike within MAP_ALIKE times (a grid stretched further leans or tapers)
MAP_CORNER = (40.0, 140.0)
SMOOTH_DEG = 20.0
MAP_ALIKE = 2.25
#: ... and no segment of a mapped side longer than MAP_LONGEST times the finest size its faces ask for
MAP_LONGEST = 1.4


def _ends(gmsh, c: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(start point, end point, direction leaving the start, direction leaving the end) of a curve."""
    lo, hi = gmsh.model.getParametrizationBounds(1, c)
    a, b = float(lo[0]), float(hi[0])
    pa = np.asarray(gmsh.model.getValue(1, c, [a]), dtype=float)
    pb = np.asarray(gmsh.model.getValue(1, c, [b]), dtype=float)
    da = np.asarray(gmsh.model.getDerivative(1, c, [a]), dtype=float)
    db = -np.asarray(gmsh.model.getDerivative(1, c, [b]), dtype=float)
    return pa, pb, da, db


def _four_sided(gmsh, t: int) -> tuple[list[list[int]], list[int]] | None:
    """(its four sides - each the curves along it, in loop order -, its four corner points) when the surface's one
    loop has exactly four corners and its other vertices are smooth; opposite sides alike within MAP_ALIKE; else
    None."""
    _l, loops = _curve_loops(gmsh, t)
    if len(loops) != 1:
        return None
    cl = [abs(int(c)) for c in loops[0]]
    if len(cl) < 4 or len(set(cl)) != len(cl):
        return None
    ends = {c: _ends(gmsh, c) for c in cl}
    pts = {c: [abs(int(p)) for _d, p in gmsh.model.getBoundary([(1, c)], oriented=True)] for c in cl}
    if any(len(v) != 2 or v[0] == v[1] for v in pts.values()):
        return None
    m = len(cl)
    corner_after: list[int | None] = []
    for j in range(m):
        a, b = cl[j], cl[(j + 1) % m]
        shared = set(pts[a]) & set(pts[b])
        if len(shared) != 1:
            return None
        p = shared.pop()
        # which end of each curve is at p: by where it lies (an edge may run against its own parameter)
        xp = np.asarray(gmsh.model.getValue(0, p, []), dtype=float)
        da = ends[a][2] if np.linalg.norm(ends[a][0] - xp) <= np.linalg.norm(ends[a][1] - xp) else ends[a][3]
        db = ends[b][2] if np.linalg.norm(ends[b][0] - xp) <= np.linalg.norm(ends[b][1] - xp) else ends[b][3]
        cos = float(da @ db) / max(float(np.linalg.norm(da) * np.linalg.norm(db)), 1e-300)
        ang = math.degrees(math.acos(max(-1.0, min(1.0, cos))))
        if MAP_CORNER[0] <= ang <= MAP_CORNER[1]:
            corner_after.append(p)
        elif ang >= 180.0 - SMOOTH_DEG:
            corner_after.append(None)
        else:
            return None
    at = [j for j in range(m) if corner_after[j] is not None]
    if len(at) != 4:
        return None
    sides: list[list[int]] = []
    corners: list[int] = []
    for i in range(4):
        start, stop = at[i] + 1, at[(i + 1) % 4] + 1
        idx = [(start + q) % m for q in range((stop - start) % m or m)]
        sides.append([cl[q] for q in idx])
        corners.append(int(corner_after[at[i]]))  # type: ignore[arg-type]
    length = [sum(float(gmsh.model.occ.getMass(1, c)) for c in sd) for sd in sides]
    for i in (0, 1):
        x, y = length[i], length[i + 2]
        if max(x, y) > MAP_ALIKE * max(min(x, y), 1e-300):
            return None
    return sides, corners


def _spread(total: int, lengths: list[float]) -> list[int] | None:
    """`total` segments over curves of these lengths, by length, each at least one; None when they do not fit."""
    if total < len(lengths):
        return None
    whole = sum(lengths)
    raw = [total * x / whole for x in lengths]
    out = [max(1, int(math.floor(v))) for v in raw]
    order = sorted(range(len(raw)), key=lambda i: (-(raw[i] - math.floor(raw[i])), i))
    k = 0
    while sum(out) < total:
        out[order[k % len(order)]] += 1
        k += 1
    while sum(out) > total:
        j = max((i for i in range(len(out)) if out[i] > 1), key=lambda i: (out[i] - raw[i], -i), default=None)
        if j is None:
            return None
        out[j] -= 1
    return out


def _spread_need(out: list[int], need: list[int], lengths: list[float], mm: float,
                 low_mm: float | None) -> list[int] | None:
    """SHMESH-51e: a chain's spread (out) with each piece given at least `need` segments, taken one at a time from the
    piece with the most to spare whose segments stay over the user's shortest edge; None when they do not fit."""
    out = list(out)
    for _ in range(sum(out) + 1):
        short = [i for i in range(len(out)) if out[i] < need[i]]
        if not short:
            return out
        donors = [j for j in range(len(out)) if out[j] > need[j]
                  and (low_mm is None or lengths[j] * mm / (out[j] - 1) >= low_mm)]
        if not donors:
            return None
        j = max(donors, key=lambda j: (lengths[j] / out[j] - lengths[j] / (out[j] - 1), -j))
        out[short[0]] += 1
        out[j] -= 1
    return None


def map_faces(gmsh, tag_of: dict[int, int], size_mm: dict[int, float], mm: float, *, skip: set[int],
              preset: dict[int, int], low_mm: float | None = None, fine_at: dict[int, float] | None = None,
              growth: float = 1.3, arc_mm=None, high_mm: float | None = None, wish: dict[int, int] | None = None,
              wish_long: set[int] | None = None) -> tuple[dict[int, list[int]], dict[int, int]]:
    """SHMESH-40: every four-cornered face (_four_sided) not in `skip` is meshed as a mapped grid. Opposite sides that
    are one curve each take one count, and so does every curve they share with other mapped faces (a pipe's pieces,
    a run of cells: one class of curves, one count). A class's count is its curves' mean length over the finest size
    of the faces it bounds (for an arc also `arc_mm(curve, face)`: the size the plan puts round that arc, so a pipe
    keeps its round), never so many that a segment comes out under `low_mm` (the user's shortest edge), made even when
    the class also bounds a free face (Blossom's condition there); a class holding a curve already counted (`preset`:
    a mapped strip's, a washer's) takes that count. A side that is a chain of curves takes its opposite side's count,
    spread over its curves by length. A face whose counts cannot agree (two presets in one class, a chain already
    counted otherwise) is left free, and the rest counted again. A face with a smaller piece of the CAD at a corner
    (`fine_at`: the size graded to there, mm, more than one growth step finer) stays free (a mapped grid cannot
    grade). Returns ({mapped face: its corners}, {curve: count})."""
    cand: dict[int, tuple[list[list[int]], list[int]]] = {}
    for t in sorted(tag_of):
        if t in skip:
            continue
        got = _four_sided(gmsh, t)
        if got is None:
            continue
        if fine_at and min((fine_at.get(p, math.inf) for p in got[1]), default=math.inf) * growth < size_mm[t]:
            continue
        cand[t] = got
    # nor next to a free face more than two growth steps finer (a grid cannot grade down to it; a mapped
    # neighbour shares its counts instead)
    for t in sorted(cand):
        near = {int(s) for sd in cand[t][0] for c in sd for s in gmsh.model.getAdjacencies(1, c)[0]
                if int(s) != t and int(s) in size_mm and int(s) not in cand}
        if any(size_mm[s] * growth ** 2 < size_mm[t] for s in near):
            cand.pop(t)
    length = {c: float(gmsh.model.occ.getMass(1, c)) for t in cand for sd in cand[t][0] for c in sd}
    counts: dict[int, int] = {}
    # SHMESH-47: a class whose curve stands opposite a chain of counted curves takes the chain's sum (a fillet cut at a
    # gridded web's lines carries the web's columns on to the flange): {a curve of the class: its count}
    forced: dict[int, int] = {}
    bump: dict[int, int] = {}       # SHMESH-51e: two-ring bolt box sides given two segments more (a chain beside needs them)
    boxed_by: dict[int, int] = {}   # SHMESH-51n: the chain pieces beside two-ring bolt boxes, the counts the boxes gave them
    rounds = 0
    while True:
        rounds += 1
        parent: dict[int, int] = {}

        def root(c: int, parent: dict[int, int] = parent) -> int:
            parent.setdefault(c, c)
            while parent[c] != c:
                parent[c] = parent[parent[c]]
                c = parent[c]
            return c

        single: set[int] = set()
        for _t, (sides, _p) in sorted(cand.items()):
            for i in (0, 1):
                if len(sides[i]) == 1 and len(sides[i + 2]) == 1:
                    a, b = root(sides[i][0]), root(sides[i + 2][0])
                    single |= {sides[i][0], sides[i + 2][0]}
                    if a != b:
                        parent[max(a, b)] = min(a, b)
        members: dict[int, list[int]] = {}
        for c in sorted(single):
            members.setdefault(root(c), []).append(c)
        faces_of: dict[int, set[int]] = {}
        for t, (sides, _p) in cand.items():
            for sd in sides:
                for c in sd:
                    if c in single:
                        faces_of.setdefault(root(c), set()).add(t)
        bad: set[int] = set()
        counts = {}
        newly = False
        fixed_roots: set[int] = set()       # (SHMESH-51e: classes whose count is held: a preset, forced or a box's)
        box_roots: set[int] = set()         # (... a two-ring bolt box's side's)
        for r, cs in sorted(members.items()):
            pre = {preset[c] for c in cs if c in preset} | {forced[c] for c in cs if c in forced}
            if len(pre) > 1:
                bad |= faces_of[r]
                continue
            wished = sorted({wish[c] for c in cs if c in wish}) if wish else []
            if pre and BOX_SPLITS_HELD and TRANSITION_CELLS and wish and wish_long:
                # SHMESH-51o20: a two-ring bolt box's side in a class another curve holds at another count (a narrow
                # strip's, a block preset's) keeps its own: the mapped cell beside the HOLDING curve goes free - a
                # transition cell (SHMESH-51o17) between the two counts - and the rest of the class takes the box's
                held_n = next(iter(pre))
                mine = [c for c in cs if c in wish_long and c in wish and wish[c] != held_n and c not in preset
                        and c not in forced]
                holders = [c for c in cs if c in preset or c in forced]
                near = {t_ for t_ in faces_of[r] for c in holders if any(c in sd for sd in cand[t_][0])}
                if mine and near and not any(c in sd for t_ in near for sd in cand[t_][0] for c in mine):
                    bad |= near
                    continue
            if pre:
                fixed_roots.add(r)
                n = pre.pop()
            else:
                h = min(size_mm[t] for t in faces_of[r]) / mm
                if arc_mm is not None:
                    for c in cs:
                        for t in sorted(faces_of[r]):
                            if any(c in sd for sd in cand[t][0]):
                                got = arc_mm(c, t)
                                if got is not None:
                                    h = min(h, got / mm)
                n = max(1, round(float(np.mean([length[c] for c in cs])) / h))
                # the class's longest curve no coarser than MAP_LONGEST of the size (and under the user's longest edge)
                longest = max(length[c] for c in cs)
                n = max(n, math.ceil(longest / (MAP_LONGEST * h) - 1e-9))
                if high_mm is not None:
                    n = max(n, math.ceil(longest * mm / (0.98 * high_mm) - 1e-9))
                if low_mm is not None:
                    n = max(1, min(n, math.floor(min(length[c] for c in cs) * mm / low_mm + 1e-9)))
                free_beside = any(s not in cand for c in cs for s in gmsh.model.getAdjacencies(1, c)[0] if s in tag_of)
                if n > 1 and n % 2 and free_beside and (low_mm is None or min(length[c] for c in cs) * mm / (n + 1) >= low_mm):
                    n += 1
                if len(wished) == 1:
                    # SHMESH-48q: a free box's side asks for n/4 + 2 (its own two-to-one row) where nothing holds the
                    # class, its longest curve no coarser and its shortest no finer than the class's own count allows
                    k_ = wished[0]
                    if ((longest / k_ <= MAP_LONGEST * h + 1e-12 or any(c in (wish_long or ()) for c in cs))
                            and (high_mm is None or longest * mm / k_ <= 0.98 * high_mm)
                            and (low_mm is None or min(length[c] for c in cs) * mm / k_ >= low_mm)):
                        n = k_
                        if BOX_HOLDS and wish_long and any(c in wish_long for c in cs):
                            fixed_roots.add(r)      # (SHMESH-51e: a two-ring bolt box's side holds its class)
                            box_roots.add(r)
            if BOX_KEEPS_COUNT and wish and wish_long and not pre and len(wished) == 1 and high_mm is not None:
                # SHMESH-51o15: a two-ring bolt box's side whose class takes another count because a curve of the
                # class is too long for the box's count under the user's longest edge keeps its own: the mapped cells
                # on that side go free and the side is held alone (SHMESH-51c)
                mine = [c for c in cs if c in wish_long and c in wish and wish[c] != n and c not in preset
                        and c not in forced and max(length[x] for x in cs) * mm / wish[c] > 0.98 * high_mm]
                near = {t_ for t_ in faces_of[r] for c in mine if any(c in sd for sd in cand[t_][0])}
                if near:
                    bad |= near
                    continue
            for c in cs:
                counts[c] = n
        # SHMESH-51e: a two-ring bolt box's side that is no class's (a cell beside it has a chain opposite) holds its
        # band's count; the chain opposite takes it (below)
        held: set[int] = set()
        if BOX_HOLDS and wish and wish_long:
            for _t, (sides, _p) in sorted(cand.items()):
                for sd in sides:
                    for c in sd:
                        k_ = bump.get(c, wish.get(c, 0))
                        if (c in wish_long and c in wish and c not in counts and c not in preset
                                and (high_mm is None or length[c] * mm / k_ <= 0.98 * high_mm + 1e-9)
                                and (low_mm is None or length[c] * mm / k_ >= low_mm - 1e-9)):
                            counts[c] = k_
                            held.add(c)
        if BOX_CHAIN and (held or box_roots) and rounds < MAP_ROUNDS:
            # SHMESH-51n: a chain of curves facing a two-ring bolt box's side across a cell takes its segments from
            # the box: spread over its pieces by length, each under the user's longest edge, and each piece's class
            # (through the mapped cells beyond) takes its piece's count - not the other way round (a joined face's
            # small box cell beside a joint piece of 27 mm asked 3 where the box's chain had room for 2, and the box's
            # side was given 6)
            for _t, (sides, _p) in sorted(cand.items()):
                for i in (0, 1):
                    for one, many in ((sides[i], sides[i + 2]), (sides[i + 2], sides[i])):
                        if len(one) != 1 or len(many) < 2 or any(x in preset for x in many):
                            continue
                        c1 = one[0]
                        if not (c1 in held or (c1 in single and root(c1) in box_roots)) or c1 not in counts:
                            continue
                        k_ = counts[c1]
                        lens = [length[x] for x in many]
                        if all(x in counts for x in many) and sum(counts[x] for x in many) == k_:
                            continue
                        got_s = _spread(k_, lens)
                        if got_s is not None and high_mm is not None:
                            got_s = _spread_need(got_s, [max(1, math.ceil(x * mm / (0.98 * high_mm) - 1e-9))
                                                         for x in lens], lens, mm, low_mm)
                        if got_s is None or (low_mm is not None and any(
                                x * mm / m_ < low_mm - 1e-9 for x, m_ in zip(lens, got_s, strict=True))):
                            continue
                        for x, m_ in zip(many, got_s, strict=True):
                            if x not in single or root(x) in box_roots or forced.get(x) == m_:
                                continue
                            if boxed_by.get(x, m_) != m_ or any(boxed_by.get(y, m_) != m_ for y in members[root(x)]):
                                continue        # (another box gave it another count: neither holds it)
                            forced[x] = m_
                            boxed_by[x] = m_
                            newly = True

        def agree(sa: list[int], sb: list[int], counts=counts, members=members, faces_of=faces_of,
                  fixed_roots=fixed_roots, single=single, root=root) -> bool:
            """SHMESH-51e: two opposite sides of a cell beside a two-ring bolt box whose counts disagree are made to
            agree by the classes on them that nothing holds, one segment at a time, each step the one that leaves a
            class's elements nearest its faces' size (inside the user's edges); the box's own count never moves.
            Those classes are forced (and counted again). False when no such steps exist."""
            d = sum(counts.get(c, preset.get(c, 0)) for c in sa) - sum(counts.get(c, preset.get(c, 0)) for c in sb)
            roots_a = {root(c) for c in sa if c in single}
            roots_b = {root(c) for c in sb if c in single}
            tent: dict[int, int] = {}
            for _k in range(abs(d)):
                best = None
                for side, step in ((sa, -1 if d > 0 else 1), (sb, 1 if d > 0 else -1)):
                    for c in side:
                        if c not in single or c in preset:
                            continue
                        r_ = root(c)
                        if r_ in fixed_roots or (r_ in roots_a and r_ in roots_b):
                            continue
                        n_new = tent.get(r_, counts[c]) + step
                        if n_new < 1:
                            continue
                        lens = [length[x] for x in members[r_]]
                        if high_mm is not None and max(lens) * mm / n_new > 0.98 * high_mm:
                            continue
                        if low_mm is not None and min(lens) * mm / n_new < low_mm:
                            continue
                        h_ = min(size_mm[t_] for t_ in faces_of[r_]) / mm
                        key = (abs(math.log(float(np.mean(lens)) / n_new / h_)), r_)
                        if best is None or key < best[0]:
                            best = (key, r_, n_new)
                if best is None:
                    return False
                tent[best[1]] = best[2]
            for r_, n_ in sorted(tent.items()):
                forced[members[r_][0]] = n_
            return True

        # (SHMESH-51e: with two-ring bolt boxes, rounds enough for their cells to agree one by one)
        boxy = BOX_HOLDS and bool(wish_long)
        cap = MAP_ROUNDS if boxy else 8
        # chains: a side of several curves takes its opposite side's count, spread by length
        for t, (sides, _p) in sorted(cand.items()):
            if t in bad:
                continue
            for i in (0, 1):
                sa, sb = sides[i], sides[i + 2]
                if len(sa) == 1 and len(sb) == 1:
                    continue
                known = lambda sd, counts=counts: all(c in counts or c in preset for c in sd)  # noqa: E731
                total = lambda sd, counts=counts: sum(counts.get(c, preset.get(c, 0)) for c in sd)  # noqa: E731
                # (SHMESH-51e: a cell with a two-ring bolt box's side on it, or a class such a side holds)
                near_box = BOX_HOLDS and bool(held or box_roots) and any(
                    c in held or (c in single and root(c) in box_roots) for c in sa + sb)
                for one, many in ((sa, sb), (sb, sa)):
                    if (len(one) == 1 and len(many) > 1 and one[0] in single and one[0] not in preset
                            and known(many) and total(one) != total(many)
                            and not (BOX_HOLDS and root(one[0]) in box_roots)):
                        # a single curve with a class of its own opposite a counted chain: the class takes the sum
                        if forced.get(one[0]) not in (None, total(many)):
                            bad.add(t)
                        else:
                            forced[one[0]] = total(many)
                            newly = True
                if (newly and (rounds < cap or not boxy)) or t in bad:
                    continue
                if known(sa) and known(sb):
                    if total(sa) != total(sb):
                        # (SHMESH-51e: beside a two-ring bolt box the classes on its sides that nothing holds agree)
                        if near_box and rounds < cap and agree(sa, sb):
                            newly = True
                        else:
                            bad.add(t)
                    continue
                master, chain = (sa, sb) if known(sa) else (sb, sa) if known(sb) else (None, None)
                if master is None or chain is None:
                    bad.add(t)
                    continue
                fixed = [c for c in chain if c in counts or c in preset]
                left = [c for c in chain if c not in fixed]
                spread = _spread(total(master) - total(fixed), [length[c] for c in left]) if left else None
                if boxy and left and high_mm is not None:
                    # SHMESH-51e: beside two-ring bolt boxes a chain's pieces are held under the user's longest edge
                    # (one segment on a 20 mm piece of an outline cut beside a box was past it, and the cell went free);
                    # where the box's side cannot give them that many, the side takes two more (its band's corner
                    # blocks take an even surplus, SHMESH-51d) - once
                    need = [max(1, math.ceil(length[c] * mm / (0.98 * high_mm) - 1e-9)) for c in left]
                    if spread is not None:
                        spread = _spread_need(spread, need, [length[c] for c in left], mm, low_mm)
                    if (spread is None and BOX_BUMP and len(master) == 1 and master[0] in held
                            and master[0] not in bump and counts[master[0]] >= BUMP_FROM):
                        k_ = counts[master[0]] + 2
                        if (sum(need) + total(fixed) <= k_ and rounds < cap
                                and (low_mm is None or length[master[0]] * mm / k_ >= low_mm)):
                            bump[master[0]] = k_
                            newly = True
                            continue
                if spread is None:
                    bad.add(t)
                    continue
                for c, m in zip(left, spread, strict=True):
                    counts[c] = m
        if newly and rounds < cap:
            continue            # counted again with the classes the chains set
        if not bad:
            break
        for t in bad:
            cand.pop(t)
    for c in sorted(set(counts) | {c for t in cand for sd in cand[t][0] for c in sd if c in preset}):
        counts.setdefault(c, preset.get(c, 0))
    for c, n in sorted(counts.items()):
        gmsh.model.mesh.setTransfiniteCurve(c, n + 1)
    out: dict[int, list[int]] = {}
    for t, (_s, corners) in sorted(cand.items()):
        gmsh.model.mesh.setTransfiniteSurface(t, cornerTags=corners)
        gmsh.model.mesh.setRecombine(2, t)
        if MAPPED_UNSMOOTHED:
            # SHMESH-51o83: a mapped face keeps the rows its counts draw (no Laplace smoothing on it)
            gmsh.model.mesh.setSmoothing(2, t, 0)
        out[t] = corners
    return out, counts


#: SHMESH-41: a flat face holding washers is cut into a grid of cells (a box round each washer, its sides drawn on to
#: the face's edges), so the field between the washers is mapped in rows along the face's edges. A washer whose ring
#: can meet the grid's size is an O-GRID: its ring and box in four mapped quarters (cut from the box's corners to the
#: hole), a box side carrying a quarter of the washer's count, each element BOX_SEG face sizes long, the box at least
#: BOX_GAP face sizes clear of the ring, the ring's outer elements no shorter than the face size over BOX_GROW. Any
#: other washer keeps its own ring (ST.ring_mesh) in a FREE BOX BOX_FREE face sizes out from the ring, paved from the
#: ring to the box (the transition stays inside the box). Lines closer than SNAP face sizes are one line (a box side
#: that near an edge runs to that edge); no cell edge under CELL_MIN face sizes.
BLOCKS = True

#: SHMESH-47: a grid line ending on an edge the face shares cuts the neighbour there too (else the face is not gridded)
CUT_NEIGHBOURS = True
#: SHMESH-48f: every gridded face judged on its own elements once meshed (as POLAR_CHECK judges a round plate)
GRID_CHECK = True
#: SHMESH-48g: a grid line that would cross a box stops at it (else the face is not gridded)
STOP_AT_BOXES = True
BOX_STEP = 8
#: SHMESH-48: the ring an O-grid aims at, times the hole's radius (an engineer's washer: 1.5 to 2 times its diameter)
WASHER_AIM = 1.75
BOX_SEG = (0.8, 1.4)
BOX_GAP = 0.4
BOX_GROW = 2.2
BOX_FREE = 1.0
SNAP = 0.75
#: SHMESH-48i: near lines are made one only where one place keeps every ring on them inside its box
SNAP_LIMITS = True
#: SHMESH-48k: a square hole along the frame takes its ring as cells of the grid (else a free box, SHMESH-48h)
SQUARE_GRID = False
CELL_MIN = 0.4
#: SHMESH-48m: lines kept apart (no one place suits both) move apart to this many CELL_MIN, inside their limits
GAP_MIN = 1.05
#: SHMESH-48o: the ends of outline pieces across the frame hold the lines near them
OUTLINE_PINS = True
#: SHMESH-48p: where a line stands ON such a pin and a line of the other axis is nearer the pin than GAP_MIN * CELL_MIN
#: (a short edge along the first from the pin), the near line moves clear of the pin inside its limits; where it
#: cannot, the line on the pin keeps clear of it instead
PIN_CLEAR = True
#: SHMESH-48r: a cell edge under CELL_MIN that is a piece of a drawn line between the outline (or a line it crosses)
#: and a line it crosses - never a box's own side - is dropped: the line ends there in a T and the two cells beside
#: it become one; the face is tried again (up to TRIM_ROUNDS pieces) and refused only when that does not mend it
TRIM_SHORT = True
#: SHMESH-48x: a gridded face is set against its own free mesh (meshed free on the next pass, its grid back when the
#: free mesh is no better) when more than GRID_SHORT of its elements are under the user's shortest edge, or it keeps
#: more than max(GRID_TRIAS, GRID_TRIA_SHARE of its elements) triangles. It stays free only when the free mesh is better:
#: the grid's short share over GRID_SHORT and over GRID_SHORT_RATIO x the free mesh's, or its triangles over
#: max(GRID_TRIAS, GRID_TRIA_SHARE of its elements, twice the free mesh's) - either alone
GRID_GUARD = True
GRID_SHORT = 0.03
GRID_SHORT_RATIO = 1.5
GRID_TRIAS = 4
GRID_TRIA_SHARE = 0.01
TRIM_ROUNDS = 4
#: SHMESH-48t: the trial refuses a face only for a cell edge under CELL_HARD of CELL_MIN (one that clearly cannot
#: mesh); between that and CELL_MIN the face is gridded and judged on its elements once meshed (48f). None: as before
CELL_HARD: float | None = 0.5
#: ... straight edges within PARALLEL_DEG of the face's frame count as running along it
PARALLEL_DEG = 1.0
#: SHMESH-48q: a free box's cell between its ring (n elements round) and its box is our own one row of quads, two to
#: one (box_band): each box side n/4 + 2 segments (asked of the grid's counts, never forced), the ring's quarter facing
#: the side's middle n/4, a kite at each corner. Where the counts or the quads' shapes do not suit, gmsh's paving stays
FREE_BAND = True
#: ... a box side asks for its count only where its segments come out between these face sizes
BAND_SEG = (0.6, 1.4)
#: ... and with no user limits its quads stay inside these
BAND_AIMS = (("quad", "min_angle_quad", ">=", 40.0, 30.0, 0.0), ("quad", "max_angle_quad", "<=", 140.0, 150.0, 0.0),
             ("quad", "aspect", "<=", 4.0, 6.0, 0.0))
#: ... of which at most BAND_SPARE quads may stand between a target and its outer limit (none past one)
BAND_SPARE = 2
#: ... a side with 2 (4) segments more than n/4 + 2 (a count the grid holds, e.g. a strip's) takes a second row inside:
#: a kite at its corner's ring node for each pair; its new nodes smoothed BAND_SMOOTH times. OFF: as placed, the inner
#: row's quads come out folded (Jacobian under 0, angles to 173 degrees) - not yet a usable template
BAND_TWO = False
BAND_SMOOTH = 4
#: ... and the sides ask the grid for n/4 + 2 (off: the band takes the counts the grid gives)
BAND_WISH = True
#: SHMESH-48s: a side 2 segments over n/4 + 2 takes them in a corner block (4 quads, 2 new nodes) at one of its ends.
#: OFF: taking both extra segments at one corner pulls the ring's quarter half a side off its middle (webreal: quads
#: to 33 / 150 degrees, Jacobian 0.56) - every such box was refused; the step belongs in the middle or in the grid
BAND_STEP = False


def _boxed(r_mm: float, hole_mm: float, h_mm: float, low_mm: float | None = None,
           high_mm: float | None = None, rows_fit: bool = False, strict: bool = False
           ) -> tuple[int, int, float, float] | None:
    """SHMESH-41/48: (elements round, rows, ring over hole radius, box half-side mm) of an O-grid washer in a gridded
    face, every edge of it inside a band: the user's shortest and longest edge when they gave them (the count round the
    hole is then picked to fit the band, never fewer than 8), else our own (the plan's count round the hole at least,
    box elements BOX_SEG face sizes). Searched in a fixed order: the fewest elements round, then two rows before one,
    then the ring nearest WASHER_AIM times the hole (an engineer's washer: 1.5 to 2 times its diameter) that leaves
    the box clear of it, then the box side nearest one face size a segment. The box stands clear of the ring by its
    side-middle row (no shorter than the band's low end, nor BOX_GAP face sizes); the ring's outer elements are no
    shorter than the face size over BOX_GROW. The hole's circle starts on the box's diagonal (SHMESH-45b), so any count
    a multiple of WASHER_STEP splits evenly into quarters. None when nothing fits (the washer then takes a free box)."""
    user = low_mm is not None or high_mm is not None
    if rows_fit and low_mm is not None and not strict and EDGE_MARGIN > 1.0:
        # (SHMESH-48z: a plan whose every edge clears the user's shortest edge by EDGE_MARGIN, where one fits)
        planned = _boxed(r_mm, hole_mm, h_mm, EDGE_MARGIN * float(low_mm), high_mm, rows_fit=True, strict=True)
        if planned is not None:
            return planned
    e_lo = float(low_mm or 0.0)
    e_hi = 0.98 * float(high_mm) if high_mm is not None else math.inf
    seg_lo, seg_hi = max(BOX_SEG[0] * h_mm, e_lo), min(BOX_SEG[1] * h_mm, e_hi)
    own = math.ceil(2 * math.pi * r_mm / max(hole_mm, 1e-12) - 1e-9)
    n0 = WASHER_MIN if user else max(WASHER_MIN, WASHER_STEP * math.ceil(own / WASHER_STEP))
    first: tuple[int, int, float, float] | None = None
    for n in range(n0, WASHER_MAX + 1, WASHER_STEP):
        s_hole = 2 * math.pi * r_mm / n
        if s_hole < e_lo or s_hole > e_hi:
            continue
        for rows in (2, 1):
            row_hi = min(e_hi, 1.2 * h_mm)
            gap = max(e_lo, BOX_GAP * h_mm)
            b_hi = n * seg_hi / 8
            r_lo = max(WASHER_RING[0] * r_mm, r_mm + rows * e_lo, n * h_mm / (2 * math.pi * BOX_GROW))
            r_hi = min(WASHER_RING[1] * r_mm, r_mm + rows * row_hi, n * e_hi / (2 * math.pi), b_hi - gap)
            if r_lo > r_hi:
                continue
            R0 = min(max(WASHER_AIM * r_mm, r_lo), r_hi)
            b_lo0 = max(R0 + gap, n * seg_lo / 8)
            if b_lo0 > b_hi + 1e-9:
                continue
            old = (n, rows, R0 / r_mm, min(max(n * h_mm / 8, b_lo0), b_hi))
            if not (user and rows_fit):
                return old
            first = first or old
            # (SHMESH-48u, gridded faces with the user's edges: the ring and box half-side nearest the plan's for
            # which some count of rows between them fits the edge lines, the fewest round first; SHMESH-48w: where
            # none does, the plan as before - its delivered rows are judged on the mesh (48f), never planned away: a
            # round base plate's bolt holes lost their O-grids, and the plate its rings and strips)
            for R in sorted({R0, *(float(x) for x in np.linspace(r_lo, r_hi, 9))}, key=lambda x: (abs(x - R0), x)):
                b_lo = max(R + gap, n * seg_lo / 8)
                if b_lo > b_hi + 1e-9:
                    continue
                got = _box_rows(R, min(max(n * h_mm / 8, b_lo), b_hi), b_lo, b_hi, e_lo, e_hi)
                if got is not None:
                    return n, rows, R / r_mm, got
    return None if strict else first


def _ring_box_rows(R: float, b_in: float, b_out: float, h: float, e_lo: float, e_hi: float) -> int | None:
    """SHMESH-48w: the count of rows between an O-grid's ring (radius R) and its box (nearest side b_in, farthest
    corner b_out from the hole's centre) that keeps the side-middle row no shorter than e_lo and the corner row no
    longer than e_hi - the one nearest the corner's length over the size h; None when none does."""
    k_lo = max(1, math.ceil((b_out - R) / e_hi - 1e-9)) if math.isfinite(e_hi) else 1
    k_hi = math.floor((b_in - R) / e_lo + 1e-9) if e_lo > 0 else 10 ** 6
    if k_lo > k_hi:
        return None
    return min(max(round((b_out - R) / h), k_lo), k_hi)


#: SHMESH-51o84: where no count of rows from an O-grid's ring to its box fits with our margins (48w: the shortest edge
#: times EDGE_MARGIN, then as given; the longest times 0.98), the user's own two lines decide: the count that leaves the
#: fewest elements outside them, counted on the box's quarters (a quarter's depth runs from the side's middle to its
#: corners; a mapped quarter's rows across one column of elements are even), fewer over the longest edge before fewer
#: under the shortest, then the count nearest the corner's length over the size. Before, such a washer's cuts were
#: counted by their length alone: a real column's sectors (cut 29.3 mm, side middle 10.0 mm deep) took 3 rows, 3.3 to
#: 4.8 mm across the middle, 11 of each sector's 21 elements under the user's 5 mm; 2 rows leave none under and the
#: corner rows 14.65 mm. Square boxes only (the far corner within 1% of the side times sqrt 2): on the look-alike top2
#: the asks that found no count were longer boxes (21 by 22.5 mm), whose quarter facing the long side carries 5
#: segments against the ring's 3 - no mapped cell; counting its cuts mapped it anyway, with 4 triangles in each, and
#: the plate lost its grid and its two-ring holes
RING_BOX_LEAST = True
RING_BOX_MAX_ROWS = 12


def _ring_box_least(R: float, b_in: float, b_out: float, h: float, e_lo: float, e_hi: float,
                    n_round: int) -> tuple[int, int, int]:
    """SHMESH-51o84: (rows, elements over e_hi, elements under e_lo) from an O-grid's ring (radius R) to its box (nearest
    side b_in, farthest corner b_out from the hole's centre), n_round elements round a quarter: the count with the
    fewest elements outside [e_lo, e_hi]; on a tie fewer over the longest, then the count nearest (b_out - R) / h.
    A quarter's lines across run from the ring's points (even in angle) to the box side's (even along it), from the
    side's middle to its corners (the cut, b_out - R long); k rows split each line evenly; a column of elements is
    outside a line where either of its two lines over k is."""
    x_max = math.sqrt(max(b_out * b_out - b_in * b_in, 0.0))
    t_max = math.atan2(x_max, b_in)
    m = max(4, int(n_round))
    lines = [math.hypot(-x_max + j * 2 * x_max / m - R * math.sin(-t_max + j * 2 * t_max / m),
                        b_in - R * math.cos(-t_max + j * 2 * t_max / m)) for j in range(m + 1)]
    k_top = RING_BOX_MAX_ROWS
    if e_lo > 0:
        k_top = min(k_top, max(1, math.ceil((b_out - R) / e_lo)))
    aim = max(1, round((b_out - R) / h))
    best: tuple[tuple[int, int, int], tuple[int, int, int]] | None = None
    for k in range(1, max(1, k_top) + 1):
        over = sum(k for j in range(m) if max(lines[j], lines[j + 1]) / k > e_hi)
        under = sum(k for j in range(m) if min(lines[j], lines[j + 1]) / k < e_lo)
        out = sum(k for j in range(m) if max(lines[j], lines[j + 1]) / k > e_hi
                  or min(lines[j], lines[j + 1]) / k < e_lo)
        key = (out, over, abs(k - aim))
        if best is None or key < best[0]:
            best = (key, (k, over, under))
    assert best is not None
    return best[1]


def _box_rows(R: float, b: float, b_lo: float, b_hi: float, e_lo: float, e_hi: float) -> float | None:
    """SHMESH-48u: the box half-side nearest b (inside [b_lo, b_hi]) for which some count k of rows between the
    ring and the box keeps every edge of them inside the user's band: the side-middle row (b - R) / k no shorter than
    e_lo, the corner row (b sqrt 2 - R) / k no longer than e_hi; None when there is none. (A ring 15 mm out in a box
    17.5 mm half-side had none: one row was 9.75 mm at the corners over a 7.35 mm line, two 1.25 mm at the middles
    under a 2.5 mm one.)"""
    eps = 1e-9 * max(1.0, b)
    best = None
    for k in range(1, 65):
        lo_k = max(b_lo, R + k * e_lo)
        hi_k = min(b_hi, (R + k * e_hi) / math.sqrt(2)) if math.isfinite(e_hi) else b_hi
        if lo_k <= hi_k + eps:
            x = min(max(b, lo_k), hi_k)
            if best is None or abs(x - b) < abs(best - b) - eps:
                best = x
        if R + k * e_lo > b_hi + eps:
            break
    return best


def _diagonals(u: np.ndarray, v: np.ndarray) -> list[np.ndarray]:
    """The four diagonal directions of a frame, in the order of a box's corners (-u-v, +u-v, +u+v, -u+v)."""
    return [(a * u + b * v) / math.sqrt(2.0) for a, b in ((-1, -1), (1, -1), (1, 1), (-1, 1))]


def _frame(gmsh, t: int, outer: list[int]) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """(origin, u, v) of a flat face: u along its longest straight outer edge, v square to it in the face."""
    best, u = 0.0, None
    for c in outer:
        if gmsh.model.getType(1, c) != "Line":
            continue
        pa, pb, _da, _db = _ends(gmsh, c)
        d = pb - pa
        ln = float(np.linalg.norm(d))
        if ln > best:
            best, u = ln, d / ln
    if u is None:
        return None
    lo, hi = gmsh.model.getParametrizationBounds(2, t)
    n = np.asarray(gmsh.model.getNormal(t, [0.5 * (lo[0] + hi[0]), 0.5 * (lo[1] + hi[1])]), dtype=float)[:3]
    n = n / max(float(np.linalg.norm(n)), 1e-300)
    u = u - float(u @ n) * n
    u /= max(float(np.linalg.norm(u)), 1e-300)
    v = np.cross(n, u)
    o = _samples(gmsh, outer[0], 1)[0]
    return o, u, v


#: SHMESH-51p: in a face with bolt holes, two lines no one place suits (their limits) are still one line when nearer
#: than SLIVER_MERGE face sizes (a cell edge of 0.005 mm, 0.17 mm left plates of a real column ungridded)
SLIVER_MERGE = 0.1


def _tiny(ws: list, h: float) -> float:
    """SHMESH-51p: how near two lines may be and still be made one past their limits (0 in a face without bolt
    holes: as before)."""
    return SLIVER_MERGE * h if any(w.info.get("bolt") for w in ws) else 0.0


def _cluster(values: list[tuple[float, str]], tol: float,
             limits: dict[str, tuple[float, float]] | None = None, min_gap: float = 0.0, tiny: float = 0.0
             ) -> list[tuple[float, bool, list[int]]]:
    """Values along one axis ((value, kind): kind "edge" for a straight edge of the face there, else a box side's
    index) joined where neighbours are closer than tol: [(the value - an edge's where one stands -, whether an edge
    stands there, the box sides on it)]. SHMESH-48i: with `limits` ({kind: (lowest, highest)} - where each box side
    may go and still keep its ring inside its box), a value joins its neighbours' line only where one place suits them
    all (an edge only where it suits every side on it): a chain of near lines (holes out of step, each side a little
    from the next) no longer pulls a side past its ring; the line stands at the mean, moved inside every side's
    limits."""
    out: list[tuple[float, bool, list[int]]] = []
    group: list[tuple[float, str]] = []
    span = (-math.inf, math.inf)
    # (SHMESH-48p: a "clear" value - a pin no line may run through - joins nothing; lines only keep clear of it)
    clear = [x for x, k in values if k == "clear"]
    values = [(x, k) for x, k in values if k != "clear"]
    only_pin: list[bool] = []

    def fits(kind: str, val: float) -> tuple[float, float]:
        if kind in ("edge", "pin"):
            return (val, val)
        return (limits or {}).get(kind, (-math.inf, math.inf))

    def close() -> None:
        # (SHMESH-48k: a "pin" - where a square ring of the grid meets its box - holds a line where it is, as an edge
        # does, but the line is still drawn)
        edges = [x for x, k in group if k == "edge"]
        pins = [x for x, k in group if k == "pin"]
        at = float(np.mean(edges or pins or [x for x, _k in group]))
        if limits is not None and not edges and not pins and span[0] <= span[1]:
            at = min(max(at, span[0]), span[1])
        out.append((at, bool(edges), [int(k) for _x, k in group if k not in ("edge", "pin")]))
        free = not edges and not pins and limits is not None and span[0] <= span[1]
        spans.append((span[0], span[1]) if free else (at, at))
        only_pin.append(bool(pins) and not edges and not out[-1][2])

    spans: list[tuple[float, float]] = []
    for val, kind in sorted(values) + [(math.inf, "")]:
        if group:
            lo, hi = fits(kind, val) if math.isfinite(val) else (math.inf, -math.inf)
            both = (max(span[0], lo), min(span[1], hi))
            if (limits is not None and both[0] > both[1] and tiny > 0 and val - group[-1][0] <= tiny
                    and both[0] - both[1] <= tiny):
                # SHMESH-51p: two lines (or a line and an edge) no place suits both, but nearer than `tiny`: one line
                # (a cell edge of 0.005 mm left a face ungridded); it stands where the stricter one may go
                fixed_ = [x for x, k in group + [(val, kind)] if k in ("edge", "pin")]
                at_ = float(np.mean(fixed_)) if fixed_ else 0.5 * (both[0] + both[1])
                both = (at_, at_)
            if val - group[-1][0] > tol or (limits is not None and both[0] > both[1]):
                close()
                group, span = [], (-math.inf, math.inf)
            else:
                span = both
        if math.isfinite(val):
            if not group:
                span = fits(kind, val)
            group.append((val, kind))
    if min_gap > 0:
        # SHMESH-48m: two lines kept apart (no one place suits both) but nearer than min_gap would leave a sliver
        # cell ("a cell edge 0.64 mm, under 4 mm"): each moves away from the other inside its own limits
        for i in range(len(out) - 1):
            (a, ea, sa), (b, eb, sb) = out[i], out[i + 1]
            gap = b - a
            if not (sa and sb) or gap >= min_gap or gap > tol:
                continue
            need = min_gap - gap
            down = min(0.5 * need, a - spans[i][0])
            up = min(need - down, spans[i + 1][1] - b)
            down = min(need - up, a - spans[i][0])
            if down + up >= need - 1e-12:
                out[i], out[i + 1] = (a - down, ea, sa), (b + up, eb, sb)
                spans[i] = (spans[i][0], a - down)
                spans[i + 1] = (b + up, spans[i + 1][1])
    if PIN_CLEAR and min_gap > 0:
        # SHMESH-48p: a box side nearer than min_gap to a "clear" value (a pin it may not reach, the end of a slanted
        # outline piece, where block_faces found a line of the other axis standing on it: "a cell edge 2.96 mm, under
        # 4 mm") moves clear of it inside its limits, nearer no other line than it was (nor past one); else it stays
        rep = clear
        eps_ = 1e-9 * max(1.0, min_gap)
        for i, (a, ea, sa) in enumerate(out):
            near = [p for p in rep if eps_ < abs(p - a) < min_gap - eps_]
            if not sa or not near:
                continue
            want_lo = max((p + min_gap for p in near if p < a), default=-math.inf)
            want_hi = min((p - min_gap for p in near if p > a), default=math.inf)
            to = min(max(a, want_lo), want_hi)
            if not (want_lo <= to <= want_hi and spans[i][0] - eps_ <= to <= spans[i][1] + eps_):
                continue
            if any(eps_ < abs(p - to) < min_gap - eps_ for p in rep):
                continue
            ok = True
            for k, (x, _e2, _s2) in enumerate(out):
                if k == i or only_pin[k]:
                    continue
                if (x - a) * (x - to) < 0 or abs(x - to) < min(abs(x - a), min_gap) - eps_:
                    ok = False
                    break
            if ok:
                out[i] = (to, ea, sa)
                spans[i] = (to, to)
    return out


def _remove_cells(gmsh, cells: list[int]) -> None:
    """SHMESH-48l2: take out a trial's cells with their curves and points - the entities occ.remove(cells,
    recursive=True) takes out, named one by one and taken out without gmsh's own search: that search looks through
    every face of the model for each curve and point (on a column, about 40 s of a mesh). A curve goes only when
    every surface it bounds goes, a point only when every curve it ends goes (a cutting line's piece outside the face
    keeps its end: _remove_loose takes it after), in the same order, so the model and its next tags are the same."""
    cs = {int(x) for x in cells}
    if any(len(gmsh.model.mesh.getEmbedded(2, s)) for s in cells):
        # (a cell with points or curves inside it - the CAD face's own - is left to gmsh's own search: it takes
        # those out too)
        gmsh.model.occ.remove([(2, s) for s in cells], recursive=True)
        return
    curves = sorted({abs(int(c)) for s in cells for _d, c in gmsh.model.getBoundary([(2, s)], oriented=False)})
    curves = [c for c in curves if {int(x) for x in gmsh.model.getAdjacencies(1, c)[0]} <= cs]
    cset = set(curves)
    # (every point of the cells: a closed curve - a ring's circle - names none of its own)
    pts = sorted({abs(int(q)) for d, q in gmsh.model.getBoundary([(2, s) for s in cells], combined=False,
                                                                  oriented=False, recursive=True) if d == 0})
    pts = [q for q in pts if {int(x) for x in gmsh.model.getAdjacencies(0, q)[0]} <= cset]
    gmsh.model.occ.remove([(2, s) for s in cells] + [(1, c) for c in curves] + [(0, q) for q in pts], recursive=False)


#: SHMESH-48l2: a trial's cells taken out by _remove_cells (False: gmsh's recursive remove, as before)
FAST_REMOVE = True
#: SHMESH-48l2: loose curves taken out with their points named (False: recursive)
FAST_LOOSE = True


def _remove_loose(gmsh, curves: set[int]) -> None:
    """Take out the curves among these that bound no surface (pieces of a cutting line outside the face)."""
    have = {c for _d, c in gmsh.model.getEntities(1)}
    loose = [(1, c) for c in sorted(curves & have) if not len(gmsh.model.getAdjacencies(1, c)[0])]
    ends = {c: [abs(int(q)) for _d2, q in gmsh.model.getBoundary([(1, c)], oriented=False)] for _d, c in loose}
    if loose and FAST_LOOSE and all(len(set(e)) == 2 for e in ends.values()):
        # (SHMESH-48l2: their points named too - each one only when every curve it ends goes - no search; a closed
        # curve, which names no end of its own, is left to gmsh's own search)
        lset = set(ends)
        pts = sorted({q for e in ends.values() for q in e})
        pts = [q for q in pts if {int(x) for x in gmsh.model.getAdjacencies(0, q)[0]} <= lset]
        gmsh.model.occ.remove(loose + [(0, q) for q in pts], recursive=False)
        gmsh.model.occ.synchronize()
    elif loose:
        gmsh.model.occ.remove(loose, recursive=True)
        gmsh.model.occ.synchronize()


def _mark_box(gmsh, w: Washer, corners: list[np.ndarray], cells: list[int], h: float, mm: float,
              high_mm: float | None, wish: dict[int, int], loops_of=None, wish_long: set[int] | None = None) -> None:
    """SHMESH-48q: note a free box's cell (between its ring and its box) and its corners on the washer, and ask each
    box side that is one curve for n/4 + 2 segments (`wish`: the grid's counts take it where nothing else holds the
    curve) when they come out BAND_SEG face sizes long and under the user's longest edge. `loops_of(s)`: the cells'
    curve loops, read once per cut face (SHMESH-48l2); else read here."""
    for s in cells:
        if s == w.annulus:
            continue
        lp = loops_of(s) if loops_of is not None else _curve_loops(gmsh, s)[1]
        lp = [[abs(int(c)) for c in cl] for cl in lp]
        if len(lp) == 2 and [w.ring] in lp:
            w.box = s
            outer = next(cl for cl in lp if cl != [w.ring])
            break
    else:
        return
    w.box_corners = [np.asarray(c, dtype=float) for c in corners]
    k = w.n // 4 + 2
    seg_hi = BAND_SEG[1] * h
    long_ok = bool(BOLT_BAND_LONG and w.boxed2 and high_mm is not None and wish_long is not None)
    if high_mm is not None:
        # (SHMESH-51c: a two-ring bolt hole's box sides up to the user's longest edge: its ring's outer elements are that
        # long already, and at our own 1.4 sizes a row of such boxes 60 mm apart got 6 segments a side, no band)
        seg_hi = 0.98 * float(high_mm) / mm if long_ok else min(seg_hi, 0.98 * float(high_mm) / mm)
    tol = 1e-6 * max(h, 1e-300)
    for i in range(4):
        a, b = w.box_corners[i], w.box_corners[(i + 1) % 4]
        for c in outer:
            pa, pb = _samples(gmsh, c, 1)
            if min(np.linalg.norm(pa - a) + np.linalg.norm(pb - b), np.linalg.norm(pa - b) + np.linalg.norm(pb - a)) > tol:
                continue
            seg = float(gmsh.model.occ.getMass(1, c)) / k
            if BAND_WISH and gmsh.model.getType(1, c) == "Line" and BAND_SEG[0] * h <= seg <= seg_hi:
                wish[c] = k
                # (SHMESH-51e: the sides of a box that owns them are held at that count too)
                if (long_ok or (BOX_OWNS_SIDES and w.owns)) and wish_long is not None:
                    wish_long.add(c)
            break


#: SHMESH-51d: a two-ring bolt hole's band takes box sides of unequal counts (a pitch along a plate that is not the pitch
#: across it: sides of 4 and 5, 4 and 6 segments) - each corner a kite (one segment of each side) or a block (two of
#: each, round one new node), each side's middle joined to its own share of the ring
BAND_UNEQUAL = True
#: SHMESH-51e: a free box's cell whose own band is made is judged on that band, not on gmsh's paving it replaces
BAND_JUDGED = True
#: SHMESH-51e: no line ends on a two-ring bolt hole's box: each side stays one curve and takes its band's count
BOX_OWNS_SIDES = True
BOX_OWN_ROUNDS = 6
#: SHMESH-51e: ... the joint's lines too (a joined face's lines across, carried from its neighbours' holes: held back in
#: this face, the joint keeps its split point - a smooth corner of the cell along it)
BOX_OWN_JOINT = True
#: SHMESH-51e: a mapped cell beside such a box whose sides' counts disagree takes them from the box (map_faces)
BOX_HOLDS = True
MAP_ROUNDS = 96
#: SHMESH-51e: a box side two segments more where the chain beside it needs them (else that cell is free)
BOX_BUMP = True
#: SHMESH-51o15: a two-ring bolt box's side never takes its class's other count: the cell beside it goes free instead
#: (the real column's 12-hole plate: four end boxes' sides took 6 from a class holding a 59.7 mm curve that 4 segments
#: put past the user's longest edge, their bands came out with corners of 3 degrees and were refused, and the four holes
#: fell to one ring - 12 of 12 two rings to 8)
BOX_KEEPS_COUNT = True
#: SHMESH-51o20: ... nor a count another curve of its class holds: the cell beside the holding curve goes free and
#: takes a transition between the two counts (the real column's 12-hole plate: a 47 x 4.2 mm strip two and three cells
#: out held its four end boxes' sides at 6; their bands were refused and the four holes fell to one ring)
BOX_SPLITS_HELD = True
#: SHMESH-51n: ... only for a side of BUMP_FROM segments or more: 8 round a hole (4 a side), a side of 6 left a band
#: corner of 2 to 3 degrees and the band was refused every time (lab and the real column)
BUMP_FROM = 5
#: SHMESH-51n: a chain facing a two-ring bolt box's side takes its count from the box (each piece's class follows it)
BOX_CHAIN = True
#: SHMESH-51r: a side landing inside the other plate's two-ring box column lands on that box's own corner first, where
#: that makes both chains whole (two boxes a few mm out of step across a joint: either landing on the other's nodes
#: left one of them 60 mm wide, [17.7, 42.0], where its 4 segments cover 58.8)
BOX_CORNER_MEET = True
#: SHMESH-51e: a two-ring box no bigger than keeps its band's corner kites under the user's longest edge
BOX_KITE = True
#: SHMESH-51i: a line held back from a box ends in a T on a node of the box's side (whole segments either side, so
#: the cell between maps) - its last piece slanted, the move at most one segment, the slanted cells' quads inside the
#: solver's limits and the user's quad lines (else BAND_AIMS); where no node fits the line stops one line earlier
BOX_T_NODE = True


def _t_node(x: float, y: float, end: float, far: float, i_: int, box: list, w: Washer, cands: list, present,
            lines_i: list, h: float, mm: float, low_mm, high_mm, eps: float, aims=()) -> tuple[float, float] | None:
    """SHMESH-51i: for a line at `x` (axis i_) held back from `box` (its side at `end`) and ending in a T on the line
    across at `y`: (where its end goes - a node of the box's side, n/4 + 2 segments, the nearest first, at most one
    segment away -, where its last straight piece ends: the next line across present on it beyond `y`, else `far`);
    None when no node fits (a piece under CELL_MIN sizes or the user's shortest edge beside it, a line not drawn
    there, or the slanted cells' quads outside the solver's limits or the user's quad lines)."""
    c0, c1 = float(box[2 * i_]), float(box[2 * i_ + 1])
    k = w.n // 4 + 2
    seg = (c1 - c0) / k
    on_node = min(abs(x - (c0 + j * seg)) for j in range(1, k))
    if on_node <= eps:
        return (x, y)               # (it stands on a node already: nothing moves)
    sgn = 1.0 if far > end else -1.0
    beyond = [y2 for y2 in cands if (y2 - y) * sgn > eps and present(1 - i_, y2, x)]
    y_prev = beyond[0] if beyond else far
    tall = abs(y_prev - y)
    floor = max(CELL_MIN * h, (low_mm / mm) if low_mm is not None else 0.0)
    for x_new in sorted((c0 + j * seg for j in range(1, k)), key=lambda z: (abs(z - x), z)):
        d = abs(x_new - x)
        if d > seg + eps or tall <= eps:
            break
        if not present(1 - i_, y, x_new):
            continue
        if any(abs(x2 - x) > eps and abs(x2 - x_new) < floor and present(i_, x2, y) for x2 in lines_i):
            continue
        # one element of the slanted cells (a side of the box's segment, its rows as tall), slanted and tapered
        if not _slant_fits(d, tall, seg, mm, high_mm, aims):
            continue
        return (x_new, y_prev)
    return None


def _band_split(gaps: list[int], n: int) -> tuple[list[int], list[int]] | None:
    """SHMESH-51d: (segments each corner takes of both its sides - 1 a kite, 2 a block -, each side's share of the
    ring's n segments) for box sides of these counts: every side's middle (its count less its two corners') is its
    share, at least one, the shares adding up to n; the shares nearest n/4 each, then the fewest blocks. None when no
    such split exists (the counts' sum and n of different parity, or too few segments)."""
    m = n / 4
    best = None
    for code in range(16):
        cs = [1 + ((code >> k) & 1) for k in range(4)]          # corner k sits between side k - 1 and side k
        arcs = [gaps[k] - cs[k] - cs[(k + 1) % 4] for k in range(4)]
        if min(arcs) < 1 or sum(arcs) != n:
            continue
        key = (sum(abs(a - m) for a in arcs), sum(cs), code)
        if best is None or key < best[0]:
            best = (key, cs, arcs)
    return None if best is None else (best[1], best[2])


#: SHMESH-51o17: TRANSITION CELLS - a four-sided free cell of a gridded face whose opposite sides carry n and n + 2k
#: segments (its other two alike) is meshed as a mapped grid of the coarse count with, in its row along the fine side,
#: k regular three-to-one steps (one coarse segment to three fine: four quads round two new nodes), in place of gmsh's
#: paving: a two-ring bolt box's side of 4 beside a side of 6 (a slot end's 17.9 + 20.2 + 17.9 mm, a strip's 6) was
#: paved with triangles on the real column's two long plates. Flat cells of straight sides only; refused (paving stays)
#: where a quad would come out past the solver's own limits, outside the quad lines (as a band's) or over the longest
#: edge.
TRANSITION_CELLS = True
TRANSITION_SMOOTH = 4
TRANSITION_NUDGE = 6


def _side_chain(gmsh, curves: list[int], start: np.ndarray, index: np.ndarray, xyz: np.ndarray) -> np.ndarray:
    """SHMESH-51o17: a straight side's mesh nodes in order from its corner nearest `start` (mm)."""
    nodes = _loop_nodes(gmsh, curves, index)
    d = np.linalg.norm(xyz[nodes] - start, axis=1)
    return nodes[np.argsort(d, kind="stable")]


def _along(chain_xyz: np.ndarray) -> np.ndarray:
    """Arc-length fractions along a polyline (0 to 1)."""
    seg = np.linalg.norm(np.diff(chain_xyz, axis=0), axis=1)
    acc = np.concatenate([[0.0], np.cumsum(seg)])
    return acc / max(float(acc[-1]), 1e-300)


def _at(chain_xyz: np.ndarray, frac: np.ndarray, u: float) -> np.ndarray:
    """The point at fraction u along a polyline."""
    j = int(np.clip(np.searchsorted(frac, u) - 1, 0, len(frac) - 2))
    t = (u - frac[j]) / max(float(frac[j + 1] - frac[j]), 1e-300)
    return chain_xyz[j] + t * (chain_xyz[j + 1] - chain_xyz[j])


def transition_cell(gmsh, t: int, index: np.ndarray, xyz: np.ndarray, mm: float, limits=None,
                    high_mm: float | None = None, low_mm: float | None = None) -> tuple[np.ndarray, np.ndarray]:
    """SHMESH-51o17: a free four-sided cell's own mesh - rows of the coarse count, the last row (along the fine side)
    with k three-to-one steps spread along it. Returns (the new nodes, mm; the quads, node indices - new nodes numbered
    from len(xyz)), wound as gmsh's own elements there; ValueError when the cell is not one (its shape, its counts) or
    a quad would come out past the limits (gmsh's paving then stays)."""
    from meshpipeline.engines import shell_quality as SQ

    if gmsh.model.getType(2, t) != "Plane":
        raise ValueError("not flat")
    got = _four_sided(gmsh, t)
    if got is None:
        raise ValueError("not four-sided")
    sides, corners = got
    if any(gmsh.model.getType(1, c) != "Line" for sd in sides for c in sd):
        raise ValueError("a side not straight")
    P = [np.asarray(gmsh.model.getValue(0, p, []), dtype=float) * mm for p in corners]
    ch = [_side_chain(gmsh, sides[i], P[i], index, xyz) for i in range(4)]
    cnt = [len(c_) - 1 for c_ in ch]
    if min(cnt) < 1:
        raise ValueError("a side with no segment")
    # (sides 0 and 2 the pair that differs, 1 and 3 alike: rotate so)
    if cnt[1] == cnt[3]:
        r = 0
    elif cnt[0] == cnt[2] and cnt[1] != cnt[3]:
        r = 1
    else:
        raise ValueError(f"its sides carry {cnt}: no pair alike with the other two apart")
    ch = ch[r:] + ch[:r]
    cnt = cnt[r:] + cnt[:r]
    A, R_, B, L_ = ch[0], ch[1], ch[2][::-1], ch[3][::-1]   # A, B from the corner 0 side; L, R from A to B
    if cnt[0] < cnt[2]:
        bottom, top, left, right = A, B, L_, R_
    else:
        bottom, top, left, right = B, A, L_[::-1], R_[::-1]
    n, f, m = len(bottom) - 1, len(top) - 1, len(left) - 1
    k = (f - n) // 2
    if (f - n) % 2 or k < 0 or k > n:
        raise ValueError(f"its sides carry {n} and {f} segments: not n and n + 2k")
    Xb, Xt, Xl, Xr = xyz[bottom], xyz[top], xyz[left], xyz[right]
    ub, ut, vl, vr = _along(Xb), _along(Xt), _along(Xl), _along(Xr)
    new: list[np.ndarray] = []
    base = len(xyz)

    def add(p_: np.ndarray) -> int:
        new.append(p_)
        return base + len(new) - 1

    # the k coarse segments, spread along the row, that step to three fine ones; the fine node each coarse node meets
    steps = sorted({min(n - 1, int((q + 0.5) * n / k)) for q in range(k)}) if k else []
    if len(steps) != k:
        raise ValueError("its steps do not spread")
    meet = [0]
    for i in range(n):
        meet.append(meet[-1] + (3 if i in steps else 1))
    # rows 0..m-1 of the coarse count (row 0 the coarse side), by transfinite interpolation, each coarse node drawn
    # from its place on the coarse side to the fine node it meets (so the steps' row is not sheared)
    lines: list[list[int]] = [list(bottom)]
    for j in range(1, m):
        row = [int(left[j])]
        s_j = j / max(m - 1, 1)
        for i in range(1, n):
            u = (1 - s_j) * float(ub[i]) + s_j * float(ut[meet[i]])
            v = (1 - u) * float(vl[j]) + u * float(vr[j])
            p_ = ((1 - v) * _at(Xb, ub, u) + v * _at(Xt, ut, u) + (1 - u) * Xl[j] + u * Xr[j]
                  - ((1 - u) * (1 - v) * Xb[0] + u * (1 - v) * Xb[-1] + (1 - u) * v * Xt[0] + u * v * Xt[-1]))
            row.append(add(p_))
        row.append(int(right[j]))
        lines.append(row)
    quads: list[list[int]] = []
    for j in range(m - 1):
        for i in range(n):
            quads.append([lines[j][i], lines[j][i + 1], lines[j + 1][i + 1], lines[j + 1][i]])
    # the last row: its steps
    Q_ = lines[m - 1]
    allx = np.vstack([xyz, np.array(new)]) if new else xyz
    a = 0
    for i in range(n):
        if i in steps:
            q0, q1 = allx[Q_[i]], allx[Q_[i + 1]]
            c1 = add(0.5 * ((2 * q0 + q1) / 3) + 0.5 * xyz[top[a + 1]])
            c2 = add(0.5 * ((q0 + 2 * q1) / 3) + 0.5 * xyz[top[a + 2]])
            quads += [[Q_[i], c1, int(top[a + 1]), int(top[a])], [c1, c2, int(top[a + 2]), int(top[a + 1])],
                      [c2, Q_[i + 1], int(top[a + 3]), int(top[a + 2])], [Q_[i], Q_[i + 1], c2, c1]]
            a += 3
        else:
            quads.append([Q_[i], Q_[i + 1], int(top[a + 1]), int(top[a])])
            a += 1
    if a != f:
        raise ValueError("its last row did not close")
    Qa = np.array(quads, dtype=np.int64)
    X = np.vstack([xyz, np.array(new)]) if new else xyz.copy()
    if new:
        # (the new nodes smoothed lightly: each to the mean of its neighbours; a flat cell stays in its plane)
        nb: dict[int, set[int]] = {}
        for q in Qa:
            for k_ in range(4):
                nb.setdefault(int(q[k_]), set()).update((int(q[k_ - 1]), int(q[(k_ + 1) % 4])))
        for _ in range(TRANSITION_SMOOTH):
            for v_ in range(base, len(X)):
                X[v_] = X[sorted(nb[v_])].mean(axis=0)
        # (then each new node nudged, in the cell's plane, where that eases the worst of its own quads: the steps'
        # side quads come out near Nastran's taper limit as placed)
        of: dict[int, list[int]] = {}
        for qi, q in enumerate(Qa):
            for v_ in q:
                of.setdefault(int(v_), []).append(qi)
        e_ = X[Qa[0][1]] - X[Qa[0][0]]
        f_ = X[Qa[0][3]] - X[Qa[0][0]]
        nrm = np.cross(e_, f_)
        nrm = nrm / max(float(np.linalg.norm(nrm)), 1e-300)
        d1 = e_ / max(float(np.linalg.norm(e_)), 1e-300)
        d2 = np.cross(nrm, d1)
        h_ = float(np.median(np.linalg.norm(X[Qa[:, 1]] - X[Qa[:, 0]], axis=1)))

        def worst(qs: list[int]) -> float:
            m_ = SQ.quad_measures(X, Qa[qs])
            return float(np.max(np.maximum.reduce([m_["taper"] / 0.5, (90.0 - m_["min_angle_quad"]) / 45.0,
                                                   (m_["max_angle_quad"] - 90.0) / 45.0,
                                                   1.0 - np.abs(m_["min_jacobian"])])))

        def scores() -> np.ndarray:
            m_ = SQ.quad_measures(X, Qa)
            return np.maximum.reduce([m_["taper"] / 0.5, (90.0 - m_["min_angle_quad"]) / 45.0,
                                      (m_["max_angle_quad"] - 90.0) / 45.0, 1.0 - np.abs(m_["min_jacobian"])])

        sc = scores()
        # (only the new nodes of quads near a limit move; none when no quad is)
        todo = sorted({int(v_) for qi in np.flatnonzero(sc >= 0.9) for v_ in Qa[qi] if int(v_) >= base})
        for step_ in (0.1, 0.05, 0.025) if todo else ():
            for _ in range(TRANSITION_NUDGE):
                moved = False
                for v_ in todo:
                    here = worst(of[v_])
                    p0 = X[v_].copy()
                    best = (here, p0)
                    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
                        X[v_] = p0 + step_ * h_ * (dx * d1 + dy * d2)
                        w_ = worst(of[v_])
                        if w_ < best[0] - 1e-9:
                            best = (w_, X[v_].copy())
                    X[v_] = best[1]
                    moved = moved or best[1] is not p0
                if not moved:
                    break
    # (wound as gmsh's own elements of the cell)
    types, _tags, conns = gmsh.model.mesh.getElements(2, t)
    ref = None
    for et, conn in zip(types, conns, strict=True):
        e_ = index[np.asarray(conn, dtype=np.int64)][: (4 if et == 3 else 3)]
        if len(e_) >= 3:
            ref = np.cross(xyz[e_[1]] - xyz[e_[0]], xyz[e_[2]] - xyz[e_[0]])
            break
    if ref is None:
        raise ValueError("no element of gmsh's to wind by")
    sgn = np.cross(X[Qa[:, 1]] - X[Qa[:, 0]], X[Qa[:, 3]] - X[Qa[:, 0]]) @ ref
    Qa[sgn < 0] = Qa[sgn < 0][:, ::-1]
    meas = SQ.quad_measures(X, Qa, extra=True)
    if (meas["min_jacobian"] <= 0).any() or SQ._quad_past(meas).any():
        raise ValueError("a quad past the solver's own limits")
    aims = SQ.InForce(aims=tuple(limits.aims) if limits is not None and limits.aims else BAND_AIMS)
    score = SQ.aim_score(meas, "quad", aims, len(Qa))
    if (score >= SQ.AIM_OUTER).any() or int((score > 0).sum()) > BAND_SPARE:
        raise ValueError(f"{int((score > 0).sum())} of its {len(Qa)} quads outside the quad lines")
    if high_mm is not None and float(meas["max_edge"].max()) > float(high_mm):
        raise ValueError("an edge over the longest edge asked")
    if low_mm is not None:
        # (no edge under the user's shortest edge that the cell's own sides do not already have: a step's inner edge
        # is a third of a coarse segment - on a square-hole plate the cells put 156 elements under it)
        side_min = min(float(np.linalg.norm(np.diff(xyz[c_], axis=0), axis=1).min()) for c_ in ch)
        if float(meas["min_edge"].min()) < min(float(low_mm), side_min) - 1e-9:
            raise ValueError("an edge under the shortest edge asked")
    return X[base:], Qa


def box_band(gmsh, w: Washer, index: np.ndarray, xyz: np.ndarray, mm: float, limits=None,
             high_mm: float | None = None) -> tuple[np.ndarray, np.ndarray]:
    """SHMESH-48q: a free box's own mesh between its ring and its box, in place of gmsh's paving: one row of quads, two
    to one. Each box side carries n/4 + 2 segments; the ring's quarter facing it (n/4 segments, from the ring node at
    the box corner's angle) is joined to the side's middle n/4 segments, and each corner is a kite (the ring node at
    its angle, the corner and the box nodes either side of it). No new node. Returns the quads (node indices, wound as
    ring_mesh's); ValueError when the box is not one (its cell, its counts) or a quad would come out outside the user's
    quad lines (else BAND_AIMS), the solver's own limits or the user's longest edge - gmsh's paving then stays."""
    from meshpipeline.engines import shell_quality as SQ

    if not w.box or len(w.box_corners) != 4 or w.n % 4:
        raise ValueError("no box")
    if w.box not in {int(s) for _d, s in gmsh.model.getEntities(2)}:
        raise ValueError("its box cell is gone")
    _l, lp = _curve_loops(gmsh, w.box)
    lp = [[abs(int(c)) for c in cl] for cl in lp]
    outer = [cl for cl in lp if cl != [w.ring]]
    if len(lp) != 2 or len(outer) != 1:
        raise ValueError("its box cell is not a box round its ring")
    ring = _loop_nodes(gmsh, [w.ring], index)
    box = _loop_nodes(gmsh, outer[0], index)
    n, m = w.n, w.n // 4
    N = len(box)
    if len(ring) != n or N < n + 8:
        raise ValueError(f"its ring and box carry {len(ring)} and {N} nodes, not {n} and {n + 8} or more")
    c = w.centre * mm
    e1 = w.xaxis
    e2 = np.cross(w.normal, e1)

    def angles(ids: np.ndarray) -> np.ndarray:
        rel = xyz[ids] - c
        return np.mod(np.arctan2(rel @ e2, rel @ e1), 2 * math.pi)

    a_r, a_b = angles(ring), angles(box)
    ring, a_r = ring[np.argsort(a_r, kind="stable")], np.sort(a_r)
    box, a_b = box[np.argsort(a_b, kind="stable")], np.sort(a_b)
    tol = 1e-6 * max(float(w.R), 1e-300) * mm
    cidx = []
    for q in w.box_corners:
        d = np.linalg.norm(xyz[box] - q * mm, axis=1)
        i = int(np.argmin(d))
        if float(d[i]) > tol:
            raise ValueError("no node at a box corner")
        cidx.append(i)
    cidx = sorted(set(cidx))
    if len(cidx) != 4:
        raise ValueError("the box's corners are not four nodes")
    gaps = [(cidx[(k + 1) % 4] - cidx[k]) % N for k in range(4)]
    # each side's extra segments over n/4 + 2, in pairs: one kite each in a row inside (two rows, BAND_TWO)
    fans = [(g - m - 2) // 2 for g in gaps]
    # SHMESH-48s: a side 2 segments over (its count held by the grid beside it) takes them at one of its corners: that
    # corner's block takes 3 of the side's segments (and 1 of the other side's) in 4 quads round 2 new nodes - the
    # two-to-one step on that side alone. big[c] = the side (c, or c - 1) whose 3 segments corner c takes.
    big: dict[int, int] = {}
    if BAND_STEP and any(fans) and all(f in (0, 1) for f in fans):
        for s in range(4):
            if fans[s]:
                c_ = s if s not in big else (s + 1) % 4
                if c_ in big:
                    big = {}
                    break
                big[c_] = s
        else:
            fans = [0, 0, 0, 0]
    corner = [1, 1, 1, 1]                   # SHMESH-51d: segments each corner takes of both its sides (1 kite, 2 block)
    arc_n = [m, m, m, m]                    # ... and each side's share of the ring
    if BAND_UNEQUAL and not big and (any(g - m - 2 for g in gaps)) and w.boxed2:
        got_u = _band_split(gaps, n)
        if got_u is not None:
            corner, arc_n = got_u
            fans = [0, 0, 0, 0]
    if corner == [1, 1, 1, 1] and arc_n == [m, m, m, m] and (
            any(g - m - 2 < 0 or (g - m - 2) % 2 for g in gaps) or any(f > 0 for f in fans) and (
            not BAND_TWO or any(f > 2 for f in fans))):
        raise ValueError(f"its box sides carry {gaps} segments, not {m + 2} (or {m + 2} + 2 a kite) each")

    def off(x: float) -> float:
        return abs((x + math.pi) % (2 * math.pi) - math.pi)

    # the ring node at each corner's angle (one turn of the ring for all four)
    at0 = [sum(arc_n[:k]) for k in range(4)]
    first = min(range(n), key=lambda s: (sum(off(a_r[(s + at0[k]) % n] - a_b[cidx[k]]) for k in range(4)), s))
    sides = [[int(box[(cidx[k] + q) % N]) for q in range(gaps[k] + 1)] for k in range(4)]
    arcs = [[int(ring[(first + at0[k] + j) % n]) for j in range(arc_n[k] + 1)] for k in range(4)]
    quads: list[list[int]] = []
    new: list[np.ndarray] = []
    base = len(xyz)
    if not any(fans):
        # one row: the ring's quarter to the side's middle, a kite at each corner (its tip the corner); a corner in
        # `big` a block of 4 quads round 2 new nodes taking 3 segments of its big side (SHMESH-48s)
        def add1(p_: np.ndarray) -> int:
            new.append(p_)
            return base + len(new) - 1

        start = [3 if big.get(k) == k else corner[k] for k in range(4)]            # segments corner k takes of side k
        end = [3 if big.get((k + 1) % 4) == k else corner[(k + 1) % 4] for k in range(4)]  # ... corner k + 1 of side k
        for k in range(4):
            apex, C = arcs[k][0], sides[k][0]
            if k not in big and corner[k] == 2:
                # SHMESH-51d: a block - 2 segments of each side round one new node X near the corner, 3 quads
                nxt, prv = sides[k], sides[k - 1]
                X = add1((xyz[apex] + xyz[nxt[1]] + xyz[prv[-2]] + xyz[C]) / 4.0)
                quads += [[apex, nxt[2], nxt[1], X], [X, nxt[1], C, prv[-2]], [apex, X, prv[-2], prv[-3]]]
            elif k not in big:
                quads.append([apex, sides[k][1], C, sides[k - 1][-2]])
            else:
                if big[k] == k:
                    one, three = sides[k - 1][-2], sides[k][1:4]                    # (prev side 1, this side 3)
                else:
                    one, three = sides[k][1], sides[k - 1][-2:-5:-1]               # (this side 1, prev side 3)
                X = add1(xyz[one] + xyz[three[0]] - xyz[C])
                Y = add1(0.25 * (xyz[one] + xyz[three[0]] - xyz[C] + xyz[three[1]] + xyz[three[2]] + xyz[apex]))
                quads += [[one, C, three[0], X], [X, three[0], three[1], Y], [Y, three[1], three[2], apex],
                          [apex, one, X, Y]]
            for j in range(arc_n[k]):
                t0 = start[k]
                quads.append([arcs[k][j], arcs[k][j + 1], sides[k][t0 + j + 1], sides[k][t0 + j]])
            if start[k] + arc_n[k] + end[k] != gaps[k]:
                raise ValueError("its box side's segments did not add up")
    else:
        # two rows: an inner row of new nodes (a node on each corner's diagonal, then each side's), the ring to it
        # with a kite at a ring node for each pair of extra segments, it to the box with a kite at each corner
        def add(p_: np.ndarray) -> int:
            new.append(p_)
            return base + len(new) - 1

        corner_mid = [add(0.5 * (xyz[arcs[k][0]] + xyz[sides[k][0]])) for k in range(4)]
        inner: list[list[int]] = []
        for k in range(4):
            M = gaps[k] - 2
            row = [corner_mid[k]]
            for i in range(1, M):
                tau = i / M
                pa = xyz[arcs[k][0]] + tau * (xyz[arcs[k][-1]] - xyz[arcs[k][0]])
                ra = pa - c
                ra = ra / max(float(np.linalg.norm(ra)), 1e-300) * float(w.R) * mm
                row.append(add(0.5 * (c + ra) + 0.5 * xyz[sides[k][i + 1]]))
            row.append(corner_mid[(k + 1) % 4])
            inner.append(row)
        for k in range(4):
            quads.append([corner_mid[k], sides[k][1], sides[k][0], sides[k - 1][-2]])
            for i in range(gaps[k] - 2):
                quads.append([inner[k][i], inner[k][i + 1], sides[k][i + 2], sides[k][i + 1]])
            # the kites stand on the ring nodes at the side's corners, where the band is widest (the first corner
            # round, then the second)
            at = ({0, m} if fans[k] == 2 else {0}) if fans[k] else set()
            p = 0
            if 0 in at:
                quads.append([arcs[k][0], inner[k][2], inner[k][1], inner[k][0]])
                p = 2
            for j in range(m):
                quads.append([arcs[k][j], arcs[k][j + 1], inner[k][p + 1], inner[k][p]])
                p += 1
                if j + 1 in at:
                    quads.append([arcs[k][j + 1], inner[k][p + 2], inner[k][p + 1], inner[k][p]])
                    p += 2
            if p != gaps[k] - 2:
                raise ValueError("its inner row did not close")
    Q = np.array(quads, dtype=np.int64)
    if new:
        xyz = np.vstack([xyz, np.array(new)])
        # (the new nodes smoothed lightly: each to the mean of its neighbours, in the face's plane)
        nb: dict[int, set[int]] = {}
        for q in Q:
            for k_ in range(4):
                nb.setdefault(int(q[k_]), set()).update((int(q[k_ - 1]), int(q[(k_ + 1) % 4])))
        nrm = w.normal / max(float(np.linalg.norm(w.normal)), 1e-300)
        for _ in range(BAND_SMOOTH):
            for v_ in range(base, len(xyz)):
                m_ = xyz[sorted(nb[v_])].mean(axis=0)
                xyz[v_] = m_ - float((m_ - c) @ nrm) * nrm
    if big:
        # (the corner blocks' quads wound as the ring's quarter quads are)
        nrm_ = w.normal / max(float(np.linalg.norm(w.normal)), 1e-300)
        sgn = np.cross(xyz[Q[:, 1]] - xyz[Q[:, 0]], xyz[Q[:, 3]] - xyz[Q[:, 0]]) @ nrm_
        ref = np.sign(sgn[[i for i, q in enumerate(quads) if q[0] in ring and q[1] in ring][0]])
        flip = np.sign(sgn) != ref
        Q[flip] = Q[flip][:, ::-1]
    meas = SQ.quad_measures(xyz, Q, extra=True)
    if SQ._quad_past(meas).any():
        raise ValueError("a quad past the solver's own limits")
    aims = SQ.InForce(aims=tuple(limits.aims) if limits is not None and limits.aims else BAND_AIMS)
    score = SQ.aim_score(meas, "quad", aims, len(Q))
    # (a quad between a line's target and its outer limit is what the user's allowed share is for - gmsh's paving there
    # was cut to triangles instead: at most BAND_SPARE such, none past an outer limit)
    if (score >= SQ.AIM_OUTER).any() or int((score > 0).sum()) > BAND_SPARE:
        raise ValueError(f"{int((score > 0).sum())} of its {len(Q)} quads outside the quad lines")
    if high_mm is not None and float(meas["max_edge"].max()) > float(high_mm):
        raise ValueError("an edge over the longest edge asked")
    return xyz[base:], Q


def _face_plan(gmsh, t: int, ws: list[Washer], h_mm: float, mm: float, hole_size, low_mm: float | None,
               high_mm: float | None, frame: tuple | None = None, joined: bool = False) -> dict | str:
    """SHMESH-41/47c: a face's grid plan before any line is drawn - its frame, each washer's O-grid plan, the
    straight edges and box sides along each axis, the boxes (48b: overlapping ones share a line), each box side's
    limits (48i) - or why it has none (words)."""
    h = h_mm / mm
    _l, loops = _curve_loops(gmsh, t)
    loops = [[abs(int(c)) for c in cl] for cl in loops]
    hole_loops = [sorted(w.hole) for w in ws]
    rest = [cl for cl in loops if sorted(cl) not in hole_loops]
    if len(rest) != 1:
        return "a hole without a washer in the face"
    outer = rest[0]
    # (a joined face keeps the frame it was planned in: the joints' cut split its long edges, SHMESH-47c)
    fr = frame if frame is not None else _frame(gmsh, t, outer)
    if fr is None:
        return "no straight edge to line up with"
    o, u, v = fr

    def uv(p: np.ndarray, o=o, u=u, v=v) -> tuple[float, float]:
        return float((p - o) @ u), float((p - o) @ v)

    # SHMESH-48h: a square ring (SHMESH-45f) takes a free box round its outer corners; its strips are drawn
    # in that box after the grid (imprint), so the grid is cut as for a round hole's free box
    plans = [None if w.corners else _boxed(w.r * mm, hole_size(w.r * mm, w.owner), h_mm, low_mm, high_mm,
                                           rows_fit=True)
             for w in ws]
    square_uv = {id(w): np.array([uv(q) for q in _frame_points(w)[0]]) for w in ws if w.corners}
    # SHMESH-48k: a square hole whose sides run along the frame takes its ring as cells of the grid itself - its box
    # the ring's outer square, the four strips cut by lines from the hole's corners (no free box paved round it)
    in_grid: dict[int, np.ndarray] = {}
    if SQUARE_GRID:
        tan_par = math.tan(math.radians(PARALLEL_DEG))
        for j, w in enumerate(ws):
            if not w.corners:
                continue
            pq = np.array([uv(x) for x in w.corners])
            d = np.roll(pq, -1, axis=0) - pq
            if all(min(abs(a), abs(b)) <= tan_par * max(abs(a), abs(b)) for a, b in d):
                in_grid[j] = pq
    vals: dict[str, list[tuple[float, str]]] = {"u": [], "v": []}
    pins: list[tuple[float, float]] = []        # (the outline pins as points, SHMESH-48p)
    lo = np.array([math.inf, math.inf])
    hi = -lo
    cos_par = math.cos(math.radians(PARALLEL_DEG))
    for c in outer:
        pts = _samples(gmsh, c, 16)
        q = np.array([uv(p) for p in pts])
        lo, hi = np.minimum(lo, q.min(axis=0)), np.maximum(hi, q.max(axis=0))
        along = False
        if gmsh.model.getType(1, c) == "Line":
            d = pts[-1] - pts[0]
            d = d / max(float(np.linalg.norm(d)), 1e-300)
            if abs(float(d @ u)) >= cos_par:
                vals["v"].append((float(q[:, 1].mean()), "edge"))
                along = True
            if abs(float(d @ v)) >= cos_par:
                vals["u"].append((float(q[:, 0].mean()), "edge"))
                along = True
        if OUTLINE_PINS and not along:
            # SHMESH-48o: an outline piece across the frame (a chamfer, an arc, a kink) holds a line that lands near
            # its ends there (or 48m moves the line clear): a line just past its end cut a sliver of the outline into
            # a cell ("a cell edge 0.637 mm")
            for e_ in (q[0], q[-1]):
                vals["u"].append((float(e_[0]), "pin"))
                vals["v"].append((float(e_[1]), "pin"))
                pins.append((float(e_[0]), float(e_[1])))
    boxes = []
    for j, (w, p) in enumerate(zip(ws, plans, strict=True)):
        cu, cv = uv(w.centre)
        if j in in_grid:
            pq, wd = in_grid[j], w.R - w.r
            boxes.append([float(pq[:, 0].min()) - wd, float(pq[:, 0].max()) + wd, float(pq[:, 1].min()) - wd,
                          float(pq[:, 1].max()) + wd])
            continue
        if w.corners:
            q = square_uv[id(w)]
            boxes.append([float(q[:, 0].min()) - BOX_FREE * h, float(q[:, 0].max()) + BOX_FREE * h,
                          float(q[:, 1].min()) - BOX_FREE * h, float(q[:, 1].max()) + BOX_FREE * h])
            continue
        b = p[3] / mm if p is not None else w.R + BOX_FREE * h
        if p is None and w.boxed2 and FREE_BAND:
            # SHMESH-51: a bolt hole's two rings end in elements longer than the face's: its free box is drawn so each
            # side carries the two-to-one row's n/4 + 2 segments at the longest a band segment may be (else gmsh's
            # paving from 8 long elements out to a box of short ones left triangles round it)
            seg = min(BAND_SEG[1] * h_mm, 0.98 * high_mm if high_mm is not None else math.inf) / mm
            b_band = (1 - 1e-6) * (w.n // 4 + 2) * seg / 2
            gap_b = max(BOX_GAP * h, float(low_mm or 0.0) / mm)
            if BOLT_BAND_LONG and low_mm is not None:
                # SHMESH-51c: a neighbouring hole's box side by side leaves a whole cell between them, no thinner than
                # the user's shortest edge (EDGE_MARGIN over it): boxes 56 mm wide at a 60 mm pitch left 4 mm strips
                # (half the web's elements under its 5 mm line)
                for ow in ws:
                    if ow is w:
                        continue
                    ou, ov = uv(ow.centre)
                    for d_along, d_across in ((abs(ou - cu), abs(ov - cv)), (abs(ov - cv), abs(ou - cu))):
                        if d_across < 2 * b_band and d_along > 1e-9:
                            b_band = min(b_band, 0.5 * (d_along - EDGE_MARGIN * float(low_mm) / mm))
            if BOX_KITE and high_mm is not None:
                # SHMESH-51e: no bigger than keeps its band's corner kites under the user's longest edge (the edge
                # from the ring's node at the corner's angle to the side's first node)
                k_ = w.n // 4 + 2
                e_max = 0.98 * high_mm / mm
                rc = w.R / math.sqrt(2.0)

                def kite(b_: float, k_=k_, rc=rc) -> float:
                    return math.hypot(b_ - rc, b_ - 2 * b_ / k_ - rc)

                if kite(b_band) > e_max:
                    lo_b, hi_b = w.R + gap_b, b_band
                    if kite(lo_b) <= e_max:
                        for _i in range(40):
                            mid_b = 0.5 * (lo_b + hi_b)
                            lo_b, hi_b = (mid_b, hi_b) if kite(mid_b) <= e_max else (lo_b, mid_b)
                        b_band = lo_b
            if b_band >= w.R + gap_b:
                b = min(b, b_band)
        boxes.append([cu - b, cu + b, cv - b, cv + b])
    # SHMESH-48b: boxes of neighbouring holes that overlap (rows of bolt holes closer than a box) share one
    # line halfway between them - each box is trimmed to it; the room check below keeps every ring inside
    for lo_i, hi_i, olo, ohi in ((0, 1, 2, 3), (2, 3, 0, 1)):
        for j in range(len(boxes)):
            for k in range(len(boxes)):
                bj, bk = boxes[j], boxes[k]
                cj, ck = 0.5 * (bj[lo_i] + bj[hi_i]), 0.5 * (bk[lo_i] + bk[hi_i])
                side_by_side = min(bj[ohi], bk[ohi]) - max(bj[olo], bk[olo]) > 1e-9 * h
                if j != k and cj < ck and side_by_side and bj[hi_i] > bk[lo_i]:
                    mid = 0.5 * (bj[hi_i] + bk[lo_i])
                    if cj < mid < ck:
                        bj[hi_i] = bk[lo_i] = mid
    for j, bx in enumerate(boxes):
        vals["u"] += [(bx[0], str(4 * j)), (bx[1], str(4 * j + 1))]
        vals["v"] += [(bx[2], str(4 * j + 2)), (bx[3], str(4 * j + 3))]
    for pq in in_grid.values():
        # (lines that end on such a box near where a hole side meets it end there: no sliver between them)
        vals["u"] += [(float(pq[:, 0].min()), "pin"), (float(pq[:, 0].max()), "pin")]
        vals["v"] += [(float(pq[:, 1].min()), "pin"), (float(pq[:, 1].max()), "pin")]
    # SHMESH-48i: where each box side may go when near lines are made one - no nearer its ring than the room
    # check below asks, no further than SNAP from where it was, an O-grid box no longer or shorter than its
    # elements allow (each side half the slack)
    limits: dict[str, tuple[float, float]] = {}
    two_face = any(w.boxed2 for w in ws)    # (SHMESH-51e: the face holds two-ring bolt holes)
    if SNAP_LIMITS:
        for j, (w, p, bx) in enumerate(zip(ws, plans, boxes, strict=True)):
            cu, cv = uv(w.centre)
            grow_sq = None
            if j in in_grid:
                # the ring's row no narrower than 0.6 of its width, no wider than 1.4 face sizes
                pq, wd = in_grid[j], w.R - w.r
                inner = [float(pq[:, 0].min()), float(pq[:, 0].max()), float(pq[:, 1].min()), float(pq[:, 1].max())]
                need = 0.6 * wd
                grow_sq = max(0.0, min(SNAP * h, 1.4 * h - wd))
            elif w.corners:
                q = square_uv[id(w)]
                inner = [float(q[:, 0].min()), float(q[:, 0].max()), float(q[:, 1].min()),
                         float(q[:, 1].max())]
                need = 0.4 * h
            else:
                inner = [cu, cu, cv, cv]
                need = (p[2] * w.r + 0.3 * h) if p is not None else (w.R + 0.4 * h)
            need += 1e-6 * h        # (a side moved onto its limit passes the check below)
            kite_grow: dict[int, float] = {}
            for ax_i in (0, 1):
                lo_s, hi_s = 2 * ax_i, 2 * ax_i + 1
                grow_, shrink_ = SNAP * h, SNAP * h
                if grow_sq is not None:
                    grow_ = grow_sq
                if p is not None:
                    side = bx[hi_s] - bx[lo_s]
                    grow_ = min(grow_, max(0.0, 0.5 * (1.5 * h * p[0] / 4 - side)))
                    shrink_ = min(shrink_, max(0.0, 0.5 * (side - 0.6 * h * p[0] / 4)))
                elif (w.boxed2 or (BOX_OWNS_SIDES and w.info.get("bolt") and (two_face or (BOLT_BOX_KITE and joined)))) \
                        and FREE_BAND and high_mm is not None and not w.corners:
                    # (SHMESH-51: a bolt hole's free box grows no further than keeps its band's segments under the
                    # user's longest edge)
                    side = bx[hi_s] - bx[lo_s]
                    grow_ = min(grow_, max(0.0, 0.5 * ((w.n // 4 + 2) * 0.98 * float(high_mm) / mm - side)))
                    if BOX_KITE:
                        # (SHMESH-51e: nor further from its hole than keeps its band's corner kites under the user's
                        # longest edge - a side drawn on to a near line 2 mm out left a 15.8 mm kite edge)
                        cen = (cu, cv)[ax_i]
                        b_o = 0.5 * (bx[3 - 2 * ax_i] - bx[2 - 2 * ax_i])     # (the box's half-width the other way)
                        k_ = w.n // 4 + 2
                        s_ = 2 * b_o / k_
                        rc_ = w.R / math.sqrt(2.0)
                        e_max = 0.98 * float(high_mm) / mm

                        def kite_at(d_: float, b_o=b_o, s_=s_, rc_=rc_) -> float:
                            return max(math.hypot(b_o - s_ - rc_, d_ - rc_), math.hypot(b_o - rc_, d_ - s_ - rc_))

                        for q_ in (lo_s, hi_s):
                            d0 = abs(bx[q_] - cen)
                            if kite_at(d0) > e_max:
                                g_ = 0.0
                            else:
                                lo_d, hi_d = d0, d0 + grow_
                                for _i in range(40):
                                    md = 0.5 * (lo_d + hi_d)
                                    lo_d, hi_d = (md, hi_d) if kite_at(md) <= e_max else (lo_d, md)
                                g_ = lo_d - d0
                            kite_grow[q_] = g_
                limits[str(4 * j + lo_s)] = (bx[lo_s] - min(grow_, kite_grow.get(lo_s, grow_)),
                                             min(bx[lo_s] + shrink_, inner[lo_s] - need))
                limits[str(4 * j + hi_s)] = (max(bx[hi_s] - shrink_, inner[hi_s] + need),
                                             bx[hi_s] + min(grow_, kite_grow.get(hi_s, grow_)))
    return {"h": h, "loops": loops, "outer": outer, "frame": (o, u, v), "uv": uv, "plans": plans,
            "square_uv": square_uv, "vals": vals, "lo": lo, "hi": hi, "boxes": boxes, "limits": limits,
            "in_grid": in_grid, "pins": pins}


#: SHMESH-47c: faces joined along their grids' lines (a channel's web and flanges, and the narrow flat strips between
#: them) are cut across at the same places, each line carried across the strips: else each face's lines ended on the
#: joint, cut the strip beside it on one side only, and the strip came out in triangles ("its lines cut a joined face
#: into triangles"), or a face beside another with holes was not gridded at all
JOIN = True
#: ... faces whose frames run this near one way (cosine) are one group: their lines are drawn at one place each
JOIN_COS = 1.0 - 1e-9
#: SHMESH-47d: a face beside another face with holes splits their shared edges where its lines meet them first
JOIN_BESIDE = True
#: ... and through narrow flat strips between them (off: on the lab parts it put triangles in gridded faces and past
#: Nastran on one; the real channel has none)
JOIN_STRIPS = False
#: SHMESH-51m: where two joined faces' boxes both stand right beside their joint, a box side's line that would land on
#: the joint inside the other face's two-ring bolt box column lands on a node of that box's side instead (its last
#: piece, from its box's corner to the joint, slanted), so the chain the joint makes beside that box takes whole
#: segments of the box's count
JOINT_NODE = True


def _slant_fits(d: float, tall: float, seg: float, mm: float, high_mm: float | None, aims=(),
                edge: bool = True) -> bool:
    """SHMESH-51i/51m: one element of the cells a line's slanted last piece makes (a segment `seg` wide, as tall as
    its rows over `tall`, its top moved `d` along) inside the solver's limits, the user's quad lines (else BAND_AIMS)
    and (`edge`) under the user's longest edge."""
    from meshpipeline.engines import shell_quality as SQ

    e = tall / max(1, round(tall / seg))
    dd = d * e / tall
    xyz = np.array([[0.0, 0.0, 0.0], [seg, 0.0, 0.0], [seg + dd, e, 0.0], [dd, e, 0.0], [seg, e, 0.0],
                    [0.0, e, 0.0]]) * mm
    quads = np.array([[0, 1, 2, 3], [0, 1, 2, 5], [0, 1, 4, 3]])
    meas = SQ.quad_measures(xyz, quads, extra=True)
    score = SQ.aim_score(meas, "quad", SQ.InForce(aims=tuple(aims) if aims else BAND_AIMS), len(quads))
    if SQ._quad_past(meas).any() or (score > 0).any():
        return False
    return not edge or high_mm is None or float(meas["max_edge"].max()) <= 0.98 * float(high_mm)


def _joint_nodes(gmsh, members: list[int], plan: dict[int, dict], frame_of: dict[int, tuple[float, float]],
                 out: list, big: int, axis: np.ndarray, junctions: set[int], h: float, mm: float,
                 low_mm: float | None, high_mm: float | None, aims=(), terminals: frozenset | set = frozenset()
                 ) -> tuple[dict[tuple[int, float], float | None], dict[int, list[tuple[float, float, float, np.ndarray]]],
                            dict[int, list[tuple[float, float]]]]:
    """SHMESH-51m: on a joint between two faces of a group (a straight edge along the group's axis, shared by those two
    faces only), where face L's box and face B's two-ring bolt box both stand right beside it (no line of their face
    between the box and the joint) and a side of L's box lands on the joint strictly inside B's box column, that side's
    landing moves to the nearest node of B's box side (its n/4 + 2 segments) on the side of L's box: B's cell beside
    the joint then sees a chain of whole segments (a 56 mm box side of 4 against a joint cut 17.9 + 20.2 + 17.9 by the
    other face's lines needed 2 + 2 + 2 under 15 mm, the band was given 6 on that side and refused). Taken only where
    the slanted piece's elements fit (_slant_fits), the landing keeps CELL_MIN sizes (and the user's shortest edge)
    from every other point of the joint and passes none, and every beside cell of that joint the chain changes can
    take its box's count after it (else the joint is left as it was). Returns ({(joint, place): where it lands},
    {face: [(its line, where it lands, the joint's place across its frame, the point)]}), the places along the group's
    axis, the lines in the face's own frame."""
    eps = 1e-6 * h
    floor = max(CELL_MIN * h, (low_mm / mm) if low_mm is not None else 0.0)
    e_hi = (0.98 * float(high_mm) / mm) if high_mm is not None else MAP_LONGEST * h
    fi_of = {f: fi for fi, f in enumerate(members)}
    side_at: dict[tuple[int, int], float] = {}
    for at, _e, sides in out:
        for k in sides:
            side_at[(int(k) // big, int(k) % big)] = at
    places = sorted(at for at, _e, sides in out if sides)
    snap: dict[tuple[int, float], float | None] = {}
    bends: dict[int, list[tuple[float, float, float, np.ndarray]]] = {}
    lines_moved: dict[int, list[tuple[float, float]]] = {}

    def across(f: int, seg: tuple[np.ndarray, np.ndarray]) -> float | None:
        o, _u, v = plan[f]["frame"]
        a, b = float((seg[0] - o) @ v), float((seg[1] - o) @ v)
        return 0.5 * (a + b) if abs(a - b) <= 1e-6 * h else None

    def beside(f: int, j: int, vj: float) -> tuple[float, float, float, float] | None:
        # (the box's column along the axis, its side nearest the joint across the frame, and how far that is: when no
        # line of the face stands between them)
        fi = fi_of[f]
        if (fi, 4 * j) not in side_at or (fi, 4 * j + 1) not in side_at:
            return None
        bx = plan[f]["boxes"][j]
        near = bx[2] if abs(bx[2] - vj) <= abs(bx[3] - vj) else bx[3]
        gap = abs(near - vj)
        if gap <= SNAP * h:
            return None
        # (a value within SNAP sizes of the box's side or of the joint is drawn as one with it: 12 alike boxes a
        # hair out of line are one row)
        if any((y - near) * (vj - y) > 0 and min(abs(y - near), abs(y - vj)) > SNAP * h
               for y, k in plan[f]["vals"]["v"] if k not in (str(4 * j + 2), str(4 * j + 3))):
            return None
        a0, a1 = sorted((side_at[(fi, 4 * j)], side_at[(fi, 4 * j + 1)]))
        return a0, a1, near, gap

    for c in sorted(junctions):
        seg_c = _straight(gmsh, c)
        adj = sorted({int(n) for n in gmsh.model.getAdjacencies(1, c)[0]})
        if seg_c is None or len(adj) != 2:
            continue
        ends = sorted(float(p @ axis) for p in seg_c)
        if sum(f in fi_of for f in adj) == 1 and sum(f in terminals for f in adj) == 1:
            # a joint with a face that draws no lines of its own (no holes): no point of it inside a two-ring bolt
            # box column beside it - nothing lands there (the box's face holds its line back), and the point only
            # cut the cell beside the box's side into a chain its count could not cover
            f_a = next(f for f in adj if f in fi_of)
            v_a = across(f_a, seg_c)
            if v_a is None:
                continue
            for j, (w, p) in enumerate(zip(plan[f_a]["ws"], plan[f_a]["plans"], strict=True)):
                if w.corners or j in plan[f_a]["in_grid"] or not w.boxed2 or p is not None:
                    continue
                got = beside(f_a, j, v_a)
                if got is None or not (ends[0] - eps <= got[0] and got[1] <= ends[1] + eps):
                    continue
                for s_ in places:
                    if got[0] + eps < s_ < got[1] - eps and not any(
                            int(k) // big == fi_of[f_a] for at, _e, sides in out if abs(at - s_) <= eps for k in sides):
                        snap[(c, s_)] = None
            continue
        if any(f not in fi_of for f in adj):
            continue
        vj = {f: across(f, seg_c) for f in adj}
        if any(x is None for x in vj.values()):
            continue
        cols: dict[int, list[tuple[int, tuple[float, float, float, float]]]] = {}
        for f in adj:
            for j, w in enumerate(plan[f]["ws"]):
                if w.corners or j in plan[f]["in_grid"]:
                    continue
                got = beside(f, j, vj[f])       # type: ignore[arg-type]
                if got is not None and ends[0] - eps <= got[0] and got[1] <= ends[1] + eps:
                    cols.setdefault(f, []).append((j, got))
        pts = [s for s in places if ends[0] + eps < s < ends[1] - eps]
        def fits(where: dict[float, float], cols=cols, pts=pts, ends=ends) -> list[int]:
            # (for each beside two-ring box of this joint: how many segments its chain lacks to take its box's
            # count - 0 where it can take it)
            got = sorted({where.get(s, s) for s in pts} | set(ends))
            short: list[int] = []
            for f, lst in sorted(cols.items()):
                for j, (a0, a1, _n, _g) in lst:
                    w = plan[f]["ws"][j]
                    if not w.boxed2 or plan[f]["plans"][j] is not None:
                        continue
                    k_ = w.n // 4 + 2
                    lo_, hi_ = where.get(a0, a0), where.get(a1, a1)
                    chain = [x for x in got if lo_ - eps <= x <= hi_ + eps]
                    if len(chain) < 2 or abs(chain[0] - lo_) > eps or abs(chain[-1] - hi_) > eps:
                        short.append(0)
                        continue
                    pieces = [b - a for a, b in zip(chain, chain[1:])]
                    need = sum(max(1, math.ceil(x / e_hi - 1e-9)) for x in pieces)
                    most = sum(math.floor(x / floor + 1e-9) for x in pieces) if floor > 0 else k_
                    short.append(max(0, need - k_) + max(0, k_ - most))
            return short

        moves: dict[float, float] = {}
        whole: dict[float, int] = {}            # (a place moved whole in this face: a line it only carries)
        slant: dict[float, int] = {}            # (... its last piece slanted in this face: its box's side)
        # (first each side that can land on the other box's own corner - the two lines then meet at one point of
        # the joint and both chains are whole: two boxes a few mm out of step across a joint -, then on its nodes)
        for phase in ("corner", "node"):
            for f_l in adj:
                f_b = adj[1] if f_l == adj[0] else adj[0]
                boxes_b = [(j, g) for j, g in cols.get(f_b, []) if plan[f_b]["ws"][j].boxed2
                           and plan[f_b]["plans"][j] is None]
                own_l = {s_: (j_l, g_l) for j_l, g_l in cols.get(f_l, []) for s_ in g_l[:2]}
                for j_b, (b0, b1, _near_b, _gap_b) in boxes_b:
                    k_b = plan[f_b]["ws"][j_b].n // 4 + 2
                    seg_b = (b1 - b0) / k_b
                    nodes = [b0 + i * seg_b for i in range(1, k_b)]
                    if phase == "corner":
                        if not BOX_CORNER_MEET:
                            continue
                        cands = [x for x in (b0, b1) if x not in moves]
                    else:
                        cands = nodes
                    for s in [x for x in pts if b0 + eps < x < b1 - eps and x not in moves]:
                        if min(abs(s - x) for x in nodes) <= eps:
                            continue
                        keys = [int(k) for at, _e, sides in out if abs(at - s) <= eps for k in sides]
                        if any(k // big == fi_of[f_b] for k in keys):
                            continue        # (a side of the box's own face stands there too)
                        mine = [k for k in keys if k // big == fi_of[f_l]]
                        l_side = next((g[1] for x, g in own_l.items() if abs(x - s) <= eps), None)
                        if mine and l_side is None:
                            # (a side of a box of L's further from the joint: its last piece runs from the line
                            # across nearest the joint)
                            j_l = (mine[0] % big) // 4
                            bx_l = plan[f_l]["boxes"][j_l]
                            v_l = float(vj[f_l])        # type: ignore[arg-type]
                            near_l = bx_l[2] if abs(bx_l[2] - v_l) <= abs(bx_l[3] - v_l) else bx_l[3]
                            ys_l = [y for y, _k in plan[f_l]["vals"]["v"] if (y - near_l) * (v_l - y) > 0
                                    and abs(y - v_l) > eps] + [near_l]
                            y_n = ys_l[int(np.argmin([abs(y - v_l) for y in ys_l]))]
                            if abs(y_n - v_l) <= SNAP * h:
                                continue
                            a_l = sorted(side_at.get((fi_of[f_l], 4 * j_l + q), s) for q in (0, 1))
                            l_side = (a_l[0], a_l[1], y_n, abs(y_n - v_l))
                        if l_side is not None:
                            # its box's side, from the line across it nearest the joint (its box's corner, where its
                            # box stands beside the joint) to the joint: that last piece slants, towards its box
                            l0, l1, _near_l, gap_l = l_side
                            inward = 1.0 if 0.5 * (l0 + l1) > s else -1.0
                        else:
                            # a line L only carries (no box side of its own there): moved whole, when it meets no
                            # other shared edge of L's
                            gap_l, inward = 0.0, 0.0
                        # (the move that leaves this joint's boxes the fewest segments short, no box worse off;
                        # towards L's box first, then the nearest)
                        n_ok = fits(moves)
                        best = None
                        for back, d_, to in sorted(((x - s) * inward < 0, abs(x - s), x) for x in cands):
                            others = [moves.get(x, x) for x in pts if abs(x - s) > eps] + ends
                            if any((abs(x - to) < floor - eps and not (phase == "corner" and abs(x - to) <= eps))
                                   or (x - s) * (x - to) < 0 for x in others):
                                continue
                            if d_ > seg_b + eps:
                                continue
                            if l_side is not None:
                                if not _slant_fits(d_, gap_l, seg_b, mm, high_mm, aims, edge=False):
                                    continue
                            elif not _free_run(gmsh, plan[f_l]["outer"], c, axis, (s, to), eps):
                                continue
                            moves[s] = to
                            got_f = fits(moves)
                            del moves[s]
                            if any(a_ > b_ for a_, b_ in zip(got_f, n_ok, strict=True)):
                                continue        # (it would cost another two-ring box beside the joint its count)
                            key_ = (sum(got_f), back, d_)
                            if (phase == "node" or sum(got_f) < sum(n_ok)) and (best is None or key_ < best[0]):
                                best = (key_, to)
                        if best is not None:
                            moves[s] = best[1]
                            (whole if l_side is None else slant)[s] = f_l
        if not moves:
            continue

        if sum(fits(moves)) >= sum(fits({})):
            continue
        for s, to in moves.items():
            snap[(c, s)] = to
            p3 = _at_s(seg_c, axis, to, eps)
            if p3 is None:
                continue
            for f in adj:
                off, sg = frame_of[f]
                if whole.get(s) == f:
                    lines_moved.setdefault(f, []).append(((s - off) * sg, (to - off) * sg))
                elif slant.get(s) == f:
                    bends.setdefault(f, []).append(((s - off) * sg, (to - off) * sg, float(vj[f]), p3))  # type: ignore[arg-type]
    return snap, bends, lines_moved


def _free_run(gmsh, outer: list[int], joint: int, axis: np.ndarray, at: tuple[float, ...], eps: float) -> bool:
    """SHMESH-51m: whether a line across a face's frame at these places along the axis meets no shared edge of the
    face's outline but `joint` (its other ends land on free edges: the line may move whole)."""
    for cc in outer:
        if cc == joint or len(gmsh.model.getAdjacencies(1, cc)[0]) < 2:
            continue
        q = _samples(gmsh, cc, 16) @ axis
        if any(float(q.min()) - eps <= x <= float(q.max()) + eps for x in at):
            return False
    return True


def _straight(gmsh, c: int) -> tuple[np.ndarray, np.ndarray] | None:
    """The two ends of a straight curve, else None."""
    if gmsh.model.getType(1, c) != "Line":
        return None
    a, b = _samples(gmsh, c, 1)
    return a, b


def _bridge(gmsh, f: int, axis: np.ndarray, cos_par: float) -> list[int] | None:
    """SHMESH-47c: a flat face of one loop of four straight edges, two of them along the axis (a strip between
    joined faces): those two edges, else None."""
    if gmsh.model.getType(2, f) != "Plane":
        return None
    _l, loops = _curve_loops(gmsh, f)
    if len(loops) != 1 or len(loops[0]) != 4:
        return None
    along = []
    for c in (abs(int(x)) for x in loops[0]):
        seg = _straight(gmsh, c)
        if seg is None:
            return None
        d = seg[1] - seg[0]
        if abs(float(d @ axis)) >= cos_par * max(float(np.linalg.norm(d)), 1e-300):
            along.append(c)
    return along if len(along) == 2 else None


def _at_s(seg: tuple[np.ndarray, np.ndarray], axis: np.ndarray, s: float, eps: float) -> np.ndarray | None:
    """The point of a straight edge where the axis coordinate is s, when it lies inside the edge (eps from its ends)."""
    a, b = seg
    da, db = float(a @ axis), float(b @ axis)
    if abs(db - da) <= eps:
        return None
    tt = (s - da) / (db - da)
    if tt * abs(db - da) <= eps or (1.0 - tt) * abs(db - da) <= eps:
        return None
    return a + tt * (b - a)


def _remap_washers(gmsh, ws: list[Washer], new: list[int]) -> None:
    """After a cut of their face: each washer's face and hole curves, found again among the new surfaces."""
    for w in ws:
        hit = next(((x, cl) for x in new for cl in ([abs(int(c)) for c in y] for y in _curve_loops(gmsh, x)[1])
                    if (_on_square(gmsh, w, cl) if w.corners else _on_hole(gmsh, w, cl))), None)
        if hit is None:
            raise ValueError("a washer's hole was lost in the joined faces' cut")
        w.face, w.hole = hit[0], hit[1]


def _needs_join(gmsh, t: int, fp: dict, by_face: dict[int, list[Washer]], tag_of: dict[int, int],
                gridded: set[int]) -> bool:
    """SHMESH-47c/d: whether the face's own lines (made one as its grid makes them) would end inside a straight edge
    it shares with another face with holes that is gridded too (`gridded`: planned) - the one case its grid is refused
    for its joints alone. Only such faces are joined (with the strips off); every other face is gridded as before,
    and a holed neighbour that stays free is left whole (its free mesh unchanged)."""
    o, u, v = fp["frame"]
    h = float(fp["h"])
    eps = 1e-6 * h
    lines: dict[str, list[float]] = {"u": [], "v": []}
    for ax in ("u", "v"):
        for at, edge, sides in _cluster(fp["vals"][ax], SNAP * h, fp["limits"] if SNAP_LIMITS else None,
                                        GAP_MIN * CELL_MIN * h, _tiny(fp.get("ws") or by_face.get(t, []), h)):
            if not edge and sides:
                lines[ax].append(at)
    for c in fp["outer"]:
        if not any(int(n) != t and int(n) in gridded and int(n) in tag_of
                   for n in gmsh.model.getAdjacencies(1, c)[0]):
            continue
        seg = _straight(gmsh, c)
        if seg is None:
            continue
        for axis, ax in ((u, "u"), (v, "v")):
            if any(_at_s(seg, axis, float(o @ axis) + x, eps) is not None for x in lines[ax]):
                return True
    return False


def _split_beside(gmsh, t: int, fp: dict, by_face: dict[int, list[Washer]], tag_of: dict[int, int],
                  gridded: set[int]) -> tuple[int, tuple[list[float], dict[int, float], tuple]] | None:
    """SHMESH-47d: a face to grid beside another face with holes of its own (a top plate on a column's walls, a flange
    on its web): its lines would end inside the edge they share and cut it on one side only, so the face was not
    gridded ("a cell would come out too small, a ring or a shared edge cut"). The shared edges are split first, at
    every place the face's lines (made one as its grid makes them) cross them - both faces cut together, the other
    face only at those points - so its lines end on points both faces have. Returns (face, (its lines across its
    frame, {box side: where}, its frame)) - the frame it was planned in, kept as the split edges change the longest
    edge - or None when no such edge is cut."""
    o, u, v = fp["frame"]
    h = float(fp["h"])
    lines: dict[str, list[float]] = {"u": [], "v": []}
    moved: dict[int, float] = {}
    for ax in ("u", "v"):
        for at, edge, sides in _cluster(fp["vals"][ax], SNAP * h, fp["limits"] if SNAP_LIMITS else None,
                                        GAP_MIN * CELL_MIN * h, _tiny(fp.get("ws") or by_face.get(t, []), h)):
            if ax == "u":
                moved.update({int(sd): at for sd in sides})
            if not edge and sides:
                lines[ax].append(at)
    eps = 1e-6 * h
    points: list[tuple[int, int]] = []
    beside: set[int] = set()
    occ = gmsh.model.occ
    for c in fp["outer"]:
        nbrs = [int(n) for n in gmsh.model.getAdjacencies(1, c)[0] if int(n) != t]
        if not any(n in gridded and n in tag_of for n in nbrs):
            continue
        seg = _straight(gmsh, c)
        if seg is None:
            continue
        for axis, ax in ((u, "u"), (v, "v")):
            for x in lines[ax]:
                p = _at_s(seg, axis, float(o @ axis) + x, eps)
                if p is not None:
                    points.append((0, occ.addPoint(*[float(q) for q in p])))
                    beside.update(n for n in nbrs if n in tag_of)
    if not points:
        return None
    objects = sorted({t} | beside)
    _o, omap = occ.fragment([(2, f) for f in objects], points)
    occ.synchronize()
    for f, got in zip(objects, omap[:len(objects)], strict=True):
        k = tag_of.pop(f)
        new = [x for d, x in got if d == 2]
        for x in new:
            tag_of[x] = k
        _remap_washers(gmsh, by_face.get(f, []), new)
    loose = [(0, p) for got in omap[len(objects):] for d, p in got
             if d == 0 and not len(gmsh.model.getAdjacencies(0, p)[0])]
    if loose:
        occ.remove(loose)
        occ.synchronize()
    k_t = next(tag_of[x] for d, x in omap[objects.index(t)] if d == 2)
    return k_t, (lines["u"], moved, (o, u, v))


def _join_faces(gmsh, by_face: dict[int, list[Washer]], tag_of: dict[int, int], size_mm: dict[int, float],
                mm: float, hole_size, low_mm: float | None, high_mm: float | None, no_cut: frozenset,
                cuts: set[int] | None, join_out: dict[int, set[int]] | None, frames: dict[int, tuple], aims=()
                ) -> dict[int, tuple]:
    """SHMESH-47c: groups of faces to grid joined along straight edges that run along their frames (directly, or
    through flat four-sided strips, _bridge): the box sides across the frame of every face of a group are made one
    (_cluster with each side's limits, SHMESH-48i) at one place along the group's axis, every face takes a line at
    every such place, each strip is cut straight across there and every joint split there - in one cut, before any
    face is gridded, so the faces' own lines end on the joints' points and no joint is cut on one side only. Returns
    {face (sewn index): (its lines across its frame, {box side: where})}; `tag_of`, the washers' faces and holes
    follow the cut."""
    cos_par = math.cos(math.radians(PARALLEL_DEG))
    plan: dict[int, dict] = {}
    for t, ws in sorted(by_face.items()):
        if t in tag_of and tag_of[t] not in no_cut:
            fp = _face_plan(gmsh, t, ws, size_mm[t], mm, hole_size, low_mm, high_mm, joined=True)
            if not isinstance(fp, str):
                fp["ws"] = ws
                plan[t] = fp
    joint: dict[int, tuple] = {}
    done: set[int] = set()          # (sewn indices: a cut gives a face a new tag)
    occ = gmsh.model.occ
    big = 1_000_000

    def refresh() -> None:
        """After a cut: the faces with washers under their new tags, their plans made again in the frames they had."""
        all_ws = [w for ws_ in by_face.values() for w in ws_]
        by_face.clear()
        for w in all_ws:
            by_face.setdefault(w.face, []).append(w)
        for t_old in sorted(plan):
            fp_old = plan[t_old]
            t_new = fp_old["ws"][0].face
            if t_new == t_old:
                continue
            del plan[t_old]
            fp_new = _face_plan(gmsh, t_new, fp_old["ws"], float(fp_old["h"]) * mm, mm, hole_size, low_mm, high_mm,
                                joined=True,
                                frame=fp_old["frame"])
            if not isinstance(fp_new, str):
                fp_new["ws"] = fp_old["ws"]
                plan[t_new] = fp_new

    while True:
        t0 = next((t for t in sorted(plan) if tag_of[t] not in done), None)
        if t0 is None:
            break
        if not (JOIN_STRIPS or _needs_join(gmsh, t0, plan[t0], by_face, tag_of, set(plan))):
            done.add(tag_of[t0])
            continue
        axis = plan[t0]["frame"][1]
        members: list[int] = [t0]
        bridges: dict[int, list[int]] = {}
        terminals: set[int] = set()
        junctions: set[int] = set()
        seen = {t0}
        queue = [t0]
        while queue:
            f = queue.pop(0)
            curves = plan[f]["outer"] if f in plan else bridges[f]
            for c in curves:
                seg = _straight(gmsh, c)
                if seg is None:
                    continue
                d = seg[1] - seg[0]
                if abs(float(d @ axis)) < cos_par * max(float(np.linalg.norm(d)), 1e-300):
                    continue
                for n in (int(x) for x in gmsh.model.getAdjacencies(1, c)[0]):
                    if n == f:
                        continue
                    junctions.add(c)
                    if n in seen:
                        continue
                    seen.add(n)
                    if n in plan and tag_of[n] not in done and abs(float(plan[n]["frame"][1] @ axis)) >= JOIN_COS:
                        members.append(n)
                        queue.append(n)
                        continue
                    along = (_bridge(gmsh, n, axis, cos_par) if JOIN_STRIPS and n in tag_of and n not in by_face
                             else None)
                    if along is not None:
                        bridges[n] = along
                        queue.append(n)
                    elif n in tag_of:
                        terminals.add(n)
        done |= {tag_of[f] for f in members}
        if len(members) < 2 and not bridges:
            if JOIN_BESIDE:
                got = _split_beside(gmsh, t0, plan[t0], by_face, tag_of, set(plan))
                if got is not None:
                    joint[got[0]] = got[1]
                    refresh()
            continue
        members.sort()
        h = min(float(plan[f]["h"]) for f in members)
        vals: list[tuple[float, str]] = []
        lim: dict[str, tuple[float, float]] = {}
        frame_of: dict[int, tuple[float, float]] = {}
        for fi, f in enumerate(members):
            o, u, _v = plan[f]["frame"]
            sg = 1.0 if float(u @ axis) > 0 else -1.0
            off = float(o @ axis)
            frame_of[f] = (off, sg)
            for x, kind in plan[f]["vals"]["u"]:
                if kind in ("edge", "pin"):
                    vals.append((off + sg * x, kind))
                    continue
                key = str(fi * big + int(kind))
                vals.append((off + sg * x, key))
                if kind in plan[f]["limits"]:
                    a, b = plan[f]["limits"][kind]
                    lim[key] = (off + a, off + b) if sg > 0 else (off - b, off - a)
        for along in bridges.values():
            for c in along:
                seg = _straight(gmsh, c)
                if seg is not None:
                    vals += [(float(seg[0] @ axis), "edge"), (float(seg[1] @ axis), "edge")]
        out = _cluster(vals, SNAP * h, lim if SNAP_LIMITS else None, GAP_MIN * CELL_MIN * h,
                       _tiny([w for f in members for w in plan[f]["ws"]], h))
        places = [at for at, _e, sides in out if sides]
        for fi, f in enumerate(members):
            off, sg = frame_of[f]
            hf = float(plan[f]["h"])
            own = [x for x, k in plan[f]["vals"]["u"] if k == "edge"]
            lo_x, hi_x = float(plan[f]["lo"][0]), float(plan[f]["hi"][0])
            moved = {int(k) % big: (at - off) * sg for at, _e, sides in out for k in sides if int(k) // big == fi}
            xs = sorted(x for x in ((at - off) * sg for at in places)
                        if lo_x + 1e-6 * hf < x < hi_x - 1e-6 * hf and not any(abs(x - e) <= 1e-3 * hf for e in own))
            joint[tag_of[f]] = (xs, moved, plan[f]["frame"])
        # the cut: each strip straight across at every place, every other joint split there
        eps = 1e-6 * h
        # (SHMESH-51m: a box side landing beside another face's two-ring bolt box moves on to a node of its side)
        snap: dict[tuple[int, float], float | None] = {}
        if JOINT_NODE and any(w.boxed2 for f in members for w in plan[f]["ws"]):
            snap, jbends, jmoved = _joint_nodes(gmsh, members, plan, frame_of, out, big, axis, junctions - {
                c for along in bridges.values() for c in along}, h, mm, low_mm, high_mm, aims,
                terminals={f for f in terminals if f not in by_face})
            for f, mv in jmoved.items():
                xs_, moved_, fr_ = joint[tag_of[f]][:3]
                xs_ = sorted(next((b_ for a_, b_ in mv if abs(a_ - x) <= eps), x) for x in xs_)
                joint[tag_of[f]] = (xs_, moved_, fr_)
            for f, lst in jbends.items():
                joint[tag_of[f]] = (*joint[tag_of[f]][:3], lst)
        segs: list[tuple[int, int]] = []
        points: list[tuple[int, int]] = []
        strip_edges = {c for along in bridges.values() for c in along}
        for s in places:
            for along in bridges.values():
                ends = [_at_s(sg_, axis, s, eps) for sg_ in (_straight(gmsh, c) for c in along) if sg_ is not None]
                if len(ends) == 2 and ends[0] is not None and ends[1] is not None:
                    segs.append((1, occ.addLine(occ.addPoint(*[float(x) for x in ends[0]]),
                                                occ.addPoint(*[float(x) for x in ends[1]]))))
            for c in sorted(junctions - strip_edges):
                seg = _straight(gmsh, c)
                s_at = snap.get((c, s), s)
                p = _at_s(seg, axis, s_at, eps) if seg is not None and s_at is not None else None
                if p is not None:
                    points.append((0, occ.addPoint(*[float(x) for x in p])))
        if not segs and not points:
            continue
        objects = sorted(set(members) | set(bridges) | terminals)
        owner = {f: tag_of[f] for f in objects}
        _o, omap = occ.fragment([(2, f) for f in objects], segs + points)
        occ.synchronize()
        for f, got in zip(objects, omap[:len(objects)], strict=True):
            k = tag_of.pop(f)
            new = [x for d, x in got if d == 2]
            for x in new:
                tag_of[x] = k
            _remap_washers(gmsh, by_face.get(f, []), new)
        refresh()
        drawn ={c for got in omap[len(objects):len(objects) + len(segs)] for d, c in got if d == 1}
        _remove_loose(gmsh, drawn)
        have = {c for _d, c in gmsh.model.getEntities(1)}
        if cuts is not None:
            cuts.update(drawn & have)
        loose = [(0, p) for got in omap[len(objects) + len(segs):] for d, p in got
                 if d == 0 and not len(gmsh.model.getAdjacencies(0, p)[0])]
        if loose:
            occ.remove(loose)
            occ.synchronize()
        if join_out is not None:
            # the strips cut with the group: no triangle there on a first mesh, else the group's faces are not
            # gridded (SHMESH-47b)
            strips = {owner[f] for f in bridges}
            for f in members:
                join_out.setdefault(owner[f], set()).update(strips)
    for fp_ in plan.values():
        frames.setdefault(tag_of[fp_["ws"][0].face], fp_["frame"])
    return joint


def block_faces(gmsh, washers: list[Washer], tag_of: dict[int, int], size_mm: dict[int, float], hole_size, mm: float,
                cuts: set[int] | None = None, low_mm: float | None = None, high_mm: float | None = None,
                cells_out: set[int] | None = None, no_cut: frozenset = frozenset(),
                cut_out: dict[int, set[int]] | None = None, made_out: set[int] | None = None,
                why_where: dict[str, list[int]] | None = None, join_out: dict[int, set[int]] | None = None,
                wish_out: dict[int, int] | None = None, wish_long: set[int] | None = None, quad_aims: tuple = (),
                own_bolt: frozenset = frozenset()
                ) -> tuple[list[Washer], list[Washer], dict[int, int], set[int], dict[str, int]]:
    """SHMESH-41: each flat face whose inner loops are all washer holes is cut into a grid: round each hole a box
    along the face's own frame (its longest straight edge) - an O-grid's (_boxed) or a free box - every box side drawn
    as a line right across the face; lines nearer each other than SNAP face sizes are one, and a box side that near a
    straight edge of the face is that edge. The cells come out four-cornered where the face's edges run along the
    frame, so map_faces maps them. A face is left to the free washers when a line would cross a box, a ring would not
    fit its box, an O-grid box side's elements would come out outside BOX_SEG, or a cell edge would come out under
    CELL_MIN face sizes (tried on a copy of the face first). Returns (the O-grid washers, the free-box washers - each
    with its ring and annulus drawn -, {curve: count} the grid must keep: the O-grid rings' quarters and rows, the
    O-grid holes' curves (for the arc sizes to leave alone), {why a face was not cut: count}). `cuts` gathers the box
    lines drawn here (no edge of the CAD: the nodes on them may move in the face's plane)."""
    by_face: dict[int, list[Washer]] = {}
    for w in washers:
        by_face.setdefault(w.face, []).append(w)
    ogrid: list[Washer] = []
    boxed: list[Washer] = []
    preset: dict[int, int] = {}
    holes: set[int] = set()
    why: dict[str, int] = {}

    def no(reason: str) -> None:
        why[reason] = why.get(reason, 0) + 1
        if why_where is not None:
            # the record says which face each reason came from (SHMESH-48f)
            why_where.setdefault(reason, []).append(int(tag_of.get(t, -1)))

    frames: dict[int, tuple] = {}       # (SHMESH-47d: each face keeps the frame it was planned in)
    joint: dict[int, tuple[list[float], dict[int, float], tuple]] = {}
    if JOIN:
        # SHMESH-47c: faces joined along their frames' lines (a channel's web and flanges, through the narrow strips
        # between them) are cut across at the same places - lines carried across the strips - before each is gridded
        try:
            joint = _join_faces(gmsh, by_face, tag_of, size_mm, mm, hole_size, low_mm, high_mm, no_cut, cuts,
                                join_out, frames, quad_aims)
        except Exception as exc:  # noqa: BLE001 - the faces are then gridded one by one, as before
            joint = {}
            why[f"its joined faces were not cut ({type(exc).__name__}: {exc})"[:200]] = 1
        by_face = {}
        for w in washers:
            by_face.setdefault(w.face, []).append(w)
    for t, ws in sorted(by_face.items()):
        if tag_of.get(t) in no_cut:
            # SHMESH-47b/48f: its grid broke a line or left triangles on a first mesh (there or in a face it cut)
            no("its grid left triangles or an unmet line on a first mesh")
            continue
        # SHMESH-45g: a face whose grid fails in any way loses only its own grid (its washers go on to the free
        # rings, or to none where the face was already cut); the other faces keep theirs
        try:
            h_mm = size_mm[t]
            jt = joint.get(tag_of[t])
            fp = _face_plan(gmsh, t, ws, h_mm, mm, hole_size, low_mm, high_mm, joined=jt is not None,
                            frame=jt[2] if jt else frames.get(tag_of[t]))
            if isinstance(fp, str):
                no(fp)
                continue
            h, loops, (o, u, v), uv = fp["h"], fp["loops"], fp["frame"], fp["uv"]
            plans, square_uv, vals, lo, hi = fp["plans"], fp["square_uv"], fp["vals"], fp["lo"], fp["hi"]
            boxes, limits = fp["boxes"], fp["limits"]
            boxes0 = [list(bx) for bx in boxes]
            turned: set[tuple[int, int]] = set()
            pin_notes: dict[tuple[str, float], str] = {}
            gap_ = GAP_MIN * CELL_MIN * h
            for _round in range(4 if PIN_CLEAR else 1):
                for bx, bx0 in zip(boxes, boxes0, strict=True):
                    bx[:] = bx0
                lines: dict[str, list[float]] = {"u": [], "v": []}
                for ax in ("u", "v"):
                    if ax == "u" and jt is not None:
                        # SHMESH-47c: a face joined to others along its frame's lines takes the lines across them all
                        lines["u"] = list(jt[0])
                        for sd, at in jt[1].items():
                            boxes[sd // 4][sd % 4] = at
                        continue
                    for at, edge, sides in _cluster(vals[ax], SNAP * h, limits if SNAP_LIMITS else None, gap_,
                                                    _tiny(ws, h)):
                        for sd in sides:
                            boxes[sd // 4][sd % 4] = at
                        if not edge and sides:
                            lines[ax].append(at)
                if not PIN_CLEAR:
                    break
                # SHMESH-48p: a line nearer an outline pin than gap_ (it could neither stand on it nor move clear)
                # leaves a short cell edge along the other axis's line through the pin ("a cell edge 2.96 mm"): that
                # line now keeps clear of the pin instead of standing on it
                # (only where a line of the other axis stands ON the pin: there the short edge runs along it from the
                # pin to the near line - first the near line moves clear, else the line on the pin does)
                turn = set()
                for k_, pt in enumerate(fp["pins"]):
                    for i_, ax in enumerate(("u", "v")):
                        other = 1 - i_
                        o_ax = ("u", "v")[other]
                        near_x = [x for x in lines[ax] if 1e-6 * h < abs(x - pt[i_]) < gap_ - 1e-6 * h]
                        held = any(abs(x - pt[other]) <= 1e-6 * h for x in lines[o_ax])
                        if not (near_x and held):
                            continue
                        fixed_ax = i_ == 0 and jt is not None        # (a joined face's u lines are the joint's)
                        if (k_, i_) not in turned and not fixed_ax:
                            turn.add((k_, i_))
                        elif (k_, other) not in turned and not (other == 0 and jt is not None):
                            turn.add((k_, other))
                        # (SHMESH-48r: the record says what held the near line - the shift it needed and what its
                        # limits allowed - when the face comes out refused)
                        for x in near_x:
                            to = pt[i_] + (gap_ if x > pt[i_] else -gap_)
                            spans = [limits.get(str(sd)) for sd in range(4 * len(ws))
                                     if boxes[sd // 4][sd % 4] == x and ("u", "v")[(sd % 4) // 2] == ax]
                            lo_, hi_ = (max((s[0] for s in spans if s), default=-math.inf),
                                        min((s[1] for s in spans if s), default=math.inf))
                            pin_notes[(ax, round(x * mm, 2))] = (
                                f"48p: the {ax} line at {x * mm:.2f} mm is {abs(x - pt[i_]) * mm:.2f} mm from an outline "
                                f"end at ({pt[0] * mm:.2f}, {pt[1] * mm:.2f}); clear of it is {to * mm:.2f} (a shift of "
                                f"{(to - x) * mm:+.2f}), " + ("its lines are the joint's" if fixed_ax else
                                                             f"its limits allow [{lo_ * mm:.2f}, {hi_ * mm:.2f}]"))
                if not turn:
                    break
                for k_, i_ in sorted(turn):
                    ax = ("u", "v")[i_]
                    vals[ax].remove((fp["pins"][k_][i_], "pin"))
                    vals[ax].append((fp["pins"][k_][i_], "clear"))
                    turned.add((k_, i_))
            reason = ""
            eps = 1e-6 * h
            for j, (w, p) in enumerate(zip(ws, plans, strict=True)):
                cu, cv = uv(w.centre)
                bx = boxes[j]
                R = p[2] * w.r if p is not None else w.R
                room = min(cu - bx[0], bx[1] - cu, cv - bx[2], bx[3] - cv)
                if j in fp["in_grid"]:
                    # a square ring of the grid: its row no narrower than 0.6 of its width (48k)
                    pq = fp["in_grid"][j]
                    R, room = 0.6 * (w.R - w.r) - 0.4 * h - eps, min(
                        float(pq[:, 0].min()) - bx[0], bx[1] - float(pq[:, 0].max()),
                        float(pq[:, 1].min()) - bx[2], bx[3] - float(pq[:, 1].max()))
                elif w.corners:
                    # a square ring's room: from its outer corners to the box's sides
                    q = square_uv[id(w)]
                    R, room = 0.0, min(float(q[:, 0].min()) - bx[0], bx[1] - float(q[:, 0].max()),
                                       float(q[:, 1].min()) - bx[2], bx[3] - float(q[:, 1].max()))
                if room < R + (0.3 if p is not None else 0.4) * h:
                    reason = "a ring too near an edge or another ring for its box"
                elif p is not None and not all(0.6 * h <= x / (p[0] / 4) <= 1.5 * h
                                               for x in (bx[1] - bx[0], bx[3] - bx[2])):
                    reason = "a box drawn on to an edge comes out too long or short for its ring"
                elif not STOP_AT_BOXES and (any(bx[0] + eps < x < bx[1] - eps for x in lines["u"]) or any(
                        bx[2] + eps < y < bx[3] - eps for y in lines["v"])):
                    reason = "the holes do not line up (a line would cross a box)"
                if reason:
                    break
            if reason:
                no(reason)
                continue
            pad = 0.05 * float((hi - lo).max())
            n_face = np.cross(u, v)
            corners_of = [[o + bx[0] * u + bx[2] * v, o + bx[1] * u + bx[2] * v, o + bx[1] * u + bx[3] * v,
                           o + bx[0] * u + bx[3] * v] for bx in boxes]
            # each O-grid's quarters start at its first box corner's direction
            starts = {}
            for w, p in zip(ws, plans, strict=True):
                if p is not None:
                    starts[id(w)] = _diagonals(u, v)[2]          # (u + v) / sqrt 2: where SHMESH-45b starts the hole

            trims: list[tuple[int, float, float, float]] = []   # SHMESH-48r: (axis, line, from, to) dropped
            retr: list[tuple[int, float, float, float]] = []    # SHMESH-51e: (axis, line, from, to) held back
            # SHMESH-51i: a held-back line's T moved on to a node of the box's side: (axis, line, its end) -> (where the
            # end goes on that axis, where its last straight piece ends)
            bends: dict[tuple[int, float, float], tuple[float, float]] = {}
            # SHMESH-51e: the two-ring bolt holes' free boxes own their sides: no other line ends on them
            # (and, in a face that holds two-ring bolt holes, its bolt holes taken back to one ring: their boxes too)
            # (SHMESH-51o2: or a face whose bolt holes the run tries with one ring, as its two rings left triangles)
            two_here = any(w.boxed2 for w in ws) or tag_of.get(t) in own_bolt
            owned = [j for j, (w, p) in enumerate(zip(ws, plans, strict=True))
                     if BOX_OWNS_SIDES and STOP_AT_BOXES and FREE_BAND and p is None and not w.corners
                     and (w.boxed2 or ((two_here or (BOLT_OWN_JOINED and jt is not None)) and w.info.get("bolt")))
                     and j not in fp["in_grid"]]
            for j in owned:
                ws[j].owns = True

            def line_pieces(at: float, axis_: int, start: float, stop: float, boxes=boxes,
                       eps=eps, trims=trims, retr=retr) -> list[tuple[float, float]]:
                """SHMESH-48g: a line across the face, stopped at each box it would cross (it ends on that box's
                side line, drawn right across: a T, the cells four-sided) - holes in rows that do not line up."""
                cut_at = sorted((bx[2 - 2 * axis_], bx[3 - 2 * axis_]) for bx in boxes
                                if bx[2 * axis_] + eps < at < bx[2 * axis_ + 1] - eps) if STOP_AT_BOXES else []
                # (SHMESH-48r: and with the pieces dropped for being too short taken out; SHMESH-51e: and the pieces
                # held back from a two-ring bolt hole's box)
                cut_at = sorted(cut_at + [(a_, b_) for ax_, at_, a_, b_ in trims + retr
                                          if ax_ == axis_ and abs(at_ - at) <= eps])
                out_, at0 = [], start
                for g0, g1 in cut_at:
                    if g0 > at0 + eps:
                        out_.append((at0, g0))
                    at0 = max(at0, g1)
                if stop > at0 + eps:
                    out_.append((at0, stop))
                return out_

            def own_sides(owned=owned, lines=lines, boxes=boxes, retr=retr, eps=eps, lo=lo, hi=hi, pad=pad, jt=jt,
                          h=h, line_pieces=line_pieces, bends=bends, ws=ws) -> None:
                """SHMESH-51e: a line that would end on a side of a two-ring bolt hole's box (48g stops it there, and
                the side came out a chain of pieces: 5 to 8 segments where its band needs n/4 + 2) is held back to
                the last line across it before the box, and ends there in a T on a line between cells; where none is
                left before the box the piece goes. The T must not leave a piece of that line under CELL_MIN sizes
                (else the next line back), never cuts into a box's own side, and stands on a line still drawn there
                (else it is held back again). The box's sides stay one curve each, corner to corner, and take the
                band's count (_mark_box); the count changes in the cells between the boxes (map_faces)."""
                retr.clear()
                bends.clear()
                if not owned:
                    return
                axes = ("u", "v")
                t_ends: set[tuple[int, float, float]] = set()
                for _r in range(BOX_OWN_ROUNDS):
                    pieces = {(i_, x): line_pieces(x, i_, lo[1 - i_] - pad, hi[1 - i_] + pad)
                              for i_ in (0, 1) for x in lines[axes[i_]]}

                    def present(i_: int, x: float, along: float, pieces=pieces, eps=eps) -> bool:
                        # (a line of axis i_ at x drawn through `along` on it)
                        return any(abs(x_ - x) <= eps and any(a_ - eps <= along <= b_ + eps for a_, b_ in segs_)
                                   for (k_, x_), segs_ in pieces.items() if k_ == i_)

                    add: list[tuple[int, float, float, float]] = []
                    for (i_, x), segs in sorted(pieces.items()):
                        if i_ == 0 and jt is not None and not BOX_OWN_JOINT:
                            continue            # (a joined face's lines across are the joint's: drawn as they are)
                        j_ = 1 - i_
                        # (a box side lying on this line: never cut into; its corners are where lines cross it)
                        prot = [(bx[2 * j_], bx[2 * j_ + 1]) for bx in boxes
                                if any(abs(x - bx[2 * i_ + q]) <= eps for q in (0, 1))]
                        for a_, b_ in segs:
                            for end, far in ((b_, a_), (a_, b_)):
                                if end <= lo[j_] - pad + eps or end >= hi[j_] + pad - eps:
                                    continue
                                hit_k = [k for k in owned if boxes[k][2 * i_] + eps < x < boxes[k][2 * i_ + 1] - eps and min(
                                    abs(end - boxes[k][2 * j_]), abs(end - boxes[k][2 * j_ + 1])) <= eps]
                                on_own = bool(hit_k)
                                if not on_own and not ((i_, x, end) in t_ends and not present(j_, end, x)):
                                    continue
                                sgn = 1.0 if far > end else -1.0
                                # the lines across it between this end and its far end, nearest first
                                cands = sorted((y for y in lines[axes[j_]] if (y - end) * sgn > eps and (far - y) * sgn > eps),
                                               key=lambda y, end=end: abs(y - end))  # type: ignore[misc]
                                to = far
                                bend = None
                                for y in cands:
                                    if any(p0 - eps <= y <= p1 + eps for p0, p1 in prot):
                                        to = y          # (a box's own side starts here: the line goes on as it)
                                        break
                                    if not present(j_, y, x):
                                        continue
                                    near_ = [x2 for x2 in lines[axes[i_]] if abs(x2 - x) > eps and present(i_, x2, y)]
                                    if near_ and min(abs(x2 - x) for x2 in near_) < CELL_MIN * h:
                                        continue        # (its T would leave a piece under CELL_MIN sizes)
                                    if BOX_T_NODE and hit_k:
                                        # SHMESH-51i: the T goes on to a node of the box's side (its column's line y
                                        # then takes whole segments of the side's count); none fits: the next line
                                        bend = _t_node(x, y, end, far, i_, boxes[hit_k[0]], ws[hit_k[0]], cands,
                                                       lambda i2, x2, y2: present(i2, x2, y2), lines[axes[i_]],
                                                       h, mm, low_mm, high_mm, eps, quad_aims)
                                        if bend is None:
                                            continue
                                    to = y
                                    break
                                # (never into a box's own side on this line)
                                for p0, p1 in prot:
                                    if sgn > 0 and end < p0 - eps and to > p0 + eps:
                                        to = p0
                                    elif sgn < 0 and end > p1 + eps and to < p1 - eps:
                                        to = p1
                                if abs(to - end) <= eps:
                                    continue
                                add.append((i_, x, min(end, to), max(end, to)))
                                if abs(to - far) > eps:
                                    t_ends.add((i_, x, to))
                                    if bend is not None and abs(bend[0] - x) > eps:
                                        bends[(i_, x, to)] = bend
                    if not add:
                        break
                    retr.extend(sorted(set(add)))

            # SHMESH-51m: the joint's lines whose landing moved on to a node of a two-ring box beside the joint:
            # (line, the end of its piece there) -> (the point it lands on, where its last straight piece ends)
            jbends: dict[tuple[float, float], tuple[np.ndarray, float]] = {}

            def joint_bends(jt=jt, lines=lines, lo=lo, hi=hi, pad=pad, eps=eps, line_pieces=line_pieces,
                            jbends=jbends) -> None:
                jbends.clear()
                for x, _x_new, vj, p3 in (jt[3] if jt is not None and len(jt) > 3 else []):
                    end = lo[1] - pad if abs(vj - lo[1]) <= abs(vj - hi[1]) else hi[1] + pad
                    got = next(((a_, b_) for a_, b_ in line_pieces(x, 0, lo[1] - pad, hi[1] + pad)
                                if min(abs(a_ - end), abs(b_ - end)) <= eps), None)
                    if got is None:
                        continue
                    far = got[1] if abs(got[0] - end) <= eps else got[0]
                    # (its last straight piece ends on the nearest line across it inside the face, drawn there)
                    ys = [y for y in lines["v"] if (y - vj) * (far - vj) > 0 and (far - y) * (far - vj) >= -eps
                          and any(a_ - eps <= x <= b_ + eps for a_, b_ in line_pieces(y, 1, lo[0] - pad, hi[0] + pad))]
                    if not ys:
                        continue
                    jbends[(x, end)] = (p3, min(ys, key=lambda y: abs(y - vj)))

            def tools(o=o, u=u, v=v, lo=lo, hi=hi, pad=pad, lines=lines, ws=ws, plans=plans, corners_of=corners_of,
                      line_pieces=line_pieces, jbends=jbends,
                      starts=starts, n_face=n_face, fp=fp, boxes=boxes, bends=bends, eps=eps) -> list[tuple[int, int]]:
                out = []
                occ = gmsh.model.occ

                def at_(i_: int, pos: float, along: float) -> np.ndarray:
                    # (the point of axis i_'s line at pos, along it)
                    return o + pos * u + along * v if i_ == 0 else o + along * u + pos * v

                for i_, ax_ in ((0, "u"), (1, "v")):
                    for x in lines[ax_]:
                        for y0, y1 in line_pieces(x, i_, lo[1 - i_] - pad, hi[1 - i_] + pad):
                            jb = next(((y_o, jbends[k_]) for k_ in jbends for y_o in (y0, y1) if i_ == 0
                                       and abs(k_[0] - x) <= eps and abs(k_[1] - y_o) <= eps), None)
                            if jb is not None:
                                # SHMESH-51m: straight to the last line across, then slanted to the joint's node
                                y_o, (p3, y_prev) = jb
                                y_s = y1 if y_o == y0 else y0
                                if abs(y_prev - y_s) > eps:
                                    out.append((1, occ.addLine(occ.addPoint(*at_(i_, x, y_s)),
                                                               occ.addPoint(*at_(i_, x, y_prev)))))
                                out.append((1, occ.addLine(occ.addPoint(*at_(i_, x, y_prev)),
                                                           occ.addPoint(*[float(q) for q in p3]))))
                                continue
                            # SHMESH-51i: an end moved on to a box side's node: its last piece runs there, slanted
                            got = next(((y_e, y_o, bd) for (i2, x2, y_e), bd in bends.items() if i2 == i_
                                        and abs(x2 - x) <= eps for y_o in (y0, y1) if abs(y_e - y_o) <= eps), None)
                            if got is None:
                                out.append((1, occ.addLine(occ.addPoint(*at_(i_, x, y0)), occ.addPoint(*at_(i_, x, y1)))))
                                continue
                            y_e, y_o, (x_new, y_prev) = got
                            y_s = y1 if y_o == y0 else y0
                            if abs(y_prev - y_s) > eps:
                                out.append((1, occ.addLine(occ.addPoint(*at_(i_, x, y_s)),
                                                           occ.addPoint(*at_(i_, x, y_prev)))))
                            out.append((1, occ.addLine(occ.addPoint(*at_(i_, x, y_prev)),
                                                       occ.addPoint(*at_(i_, x_new, y_e)))))
                for j_, (w, p, cs) in enumerate(zip(ws, plans, corners_of, strict=True)):
                    if j_ in fp["in_grid"]:
                        # SHMESH-48k: each hole side carried on past its corner to the box (a pinwheel of four
                        # strips, every corner square)
                        pq, bx_ = fp["in_grid"][j_], boxes[j_]
                        for i_ in range(4):
                            a_, b_ = pq[i_], pq[(i_ + 1) % 4]
                            d_ = b_ - a_
                            if abs(d_[0]) >= abs(d_[1]):
                                end = (bx_[1] if d_[0] > 0 else bx_[0], b_[1])
                            else:
                                end = (b_[0], bx_[3] if d_[1] > 0 else bx_[2])
                            out.append((1, occ.addLine(occ.addPoint(*(o + b_[0] * u + b_[1] * v)),
                                                       occ.addPoint(*(o + end[0] * u + end[1] * v)))))
                        continue
                    if w.corners:
                        continue            # (a square ring is drawn in its box after the grid)
                    R = p[2] * w.r if p is not None else w.R
                    xa = starts[id(w)] if p is not None else w.xaxis
                    out.append((1, occ.addCircle(*[float(x) for x in w.centre], float(R),
                                                 zAxis=[float(x) for x in n_face], xAxis=[float(x) for x in xa])))
                    if p is not None:
                        # SHMESH-48: each box corner joined to the ring and the hole at the frame's own diagonal (where the
                        # hole's circle starts, SHMESH-45b), so the ring's quarters are exact and no seam sits in a quarter
                        for cp, dg in zip(cs, _diagonals(u, v), strict=True):
                            q1 = occ.addPoint(*[float(x) for x in w.centre + R * dg])
                            out.append((1, occ.addLine(occ.addPoint(*cp), q1)))
                            out.append((1, occ.addLine(q1, occ.addPoint(*[float(x) for x in w.centre + 0.5 * w.r * dg]))))
                return out

            def only_small(causes_: list[str]) -> bool:
                return bool(causes_) and all(x.startswith("a cell edge") for x in causes_)

            def short_piece(c_: int, lines=lines, boxes=boxes, eps=eps, uv=uv) -> tuple[int, float, float, float] | None:
                """SHMESH-48r: the short edge as (axis, line, from, to) when it is a piece of a drawn line between
                the outline, or a line it crosses, and a line it crosses - outside every box's own sides (a box
                stays closed); else None."""
                if gmsh.model.getType(1, c_) != "Line":
                    return None
                e0, e1 = (np.array(uv(x)) for x in _samples(gmsh, c_, 1))
                for i_, ax in enumerate(("u", "v")):
                    if abs(e0[i_] - e1[i_]) > eps or not any(abs(x - e0[i_]) <= eps for x in lines[ax]):
                        continue
                    at, j_ = float(e0[i_]), 1 - i_
                    a_, b_ = sorted((float(e0[j_]), float(e1[j_])))
                    other = ("u", "v")[j_]
                    if not any(abs(x - a_) <= eps or abs(x - b_) <= eps for x in lines[other]):
                        return None             # (it crosses no line at either end: not a piece between lines)
                    for bx in boxes:
                        on_side = any(abs(at - bx[2 * i_ + q]) <= eps for q in (0, 1))
                        if on_side and bx[2 * j_] - eps <= 0.5 * (a_ + b_) <= bx[2 * j_ + 1] + eps:
                            return None         # (a piece of a box's own side)
                    return (i_, at, a_, b_)
                return None

            # tried on a copy of the face first: no edge of a cell under CELL_MIN face sizes
            shared = [c for cl in loops for c in cl if len(gmsh.model.getAdjacencies(1, c)[0]) > 1]
            for _trim in range(TRIM_ROUNDS + 1 if TRIM_SHORT else 1):
                own_sides()
                joint_bends()
                copy = gmsh.model.occ.copy([(2, t)])
                _o, cmap = gmsh.model.occ.fragment(copy, tools())
                gmsh.model.occ.synchronize()
                cells = [s for d, s in cmap[0] if d == 2]
                cut_curves = {c for got in cmap[1:] for d, c in got if d == 1}
                # SHMESH-48j: which of the three causes fired, with its numbers, goes in the reason
                causes: list[str] = []
                edges_t = {abs(int(c)) for s in cells for _d, c in gmsh.model.getBoundary([(2, s)], oriented=False)}
                if HOLE_PIECES:
                    # SHMESH-51q: a CAD piece of a hole's own circle (0.005 mm on a real column's 19.8 mm hole) is no
                    # cell edge to refuse the grid for: its ring asks for the hole drawn again as one circle
                    # (SHMESH-45b/45k), which never came while the face was refused here
                    edges_t -= {c for c in edges_t if _on_any_hole(gmsh, c, ws)}
                shortest = min(((float(gmsh.model.occ.getMass(1, c)), c) for c in sorted(edges_t)),
                               default=(math.inf, 0))
                small = shortest[0] < CELL_MIN * h
                cut_piece = short_piece(shortest[1]) if small and TRIM_SHORT else None
                # (48t: only a straight edge along the frame - a piece of a line or of the outline there - is left for
                # the face check to judge; a short O-grid diagonal or ring piece still refuses the face)
                along_frame = False
                if small and gmsh.model.getType(1, shortest[1]) == "Line":
                    d0, d1 = (np.array(uv(x)) for x in _samples(gmsh, shortest[1], 1))
                    along_frame = bool(min(abs(d0 - d1)) <= 1e-6 * h)
                if small:
                    mid_ = _samples(gmsh, shortest[1], 2)[1] * mm
                    # (and what stands round it, in the face's own frame, mm: the short edge's ends, the nearest hole's
                    # box, the lines and edges within a face size - so a real part's record pins the pair of lines)
                    ends_ = [np.round(np.array(uv(x)) * mm, 2) for x in _samples(gmsh, shortest[1], 1)]
                    mid_uv = 0.5 * (ends_[0] + ends_[1]) / mm
                    j_near = min(range(len(ws)), key=lambda j: float(np.linalg.norm(np.array(uv(ws[j].centre)) - mid_uv)))
                    near_l = {ax: sorted(round(x * mm, 2) for x in lines[ax] if abs(x - mid_uv[i_]) <= h)
                              for i_, ax in enumerate(("u", "v"))}
                    near_e = {ax: sorted(round(x * mm, 2) for x, k in vals[ax] if k == "edge" and abs(x - mid_uv[i_]) <= h)
                              for i_, ax in enumerate(("u", "v"))}
                    causes.append(f"a cell edge {shortest[0] * mm:.3g} mm, under {CELL_MIN * h_mm:.3g} mm, at "
                                  f"({mid_[0]:.0f}, {mid_[1]:.0f}, {mid_[2]:.0f}) mm [face frame mm: ends "
                                  f"{[list(map(float, e)) for e in ends_]}, nearest hole box "
                                  f"{[round(x * mm, 2) for x in boxes[j_near]]} (hole {2 * ws[j_near].r * mm:.3g} mm), "
                                  f"lines {near_l}, edges {near_e}]")
                # and every ring came out as drawn: one circle round a free box's hole, four quarters round an O-grid's
                trial = sorted({abs(int(c)) for s in cells for _d, c in gmsh.model.getBoundary([(2, s)], oriented=False)})
                # a line that ends inside an edge the face shares with another face cuts that edge: the other face is cut
                # with it (at that point only), else it would keep the edge whole - a crack at the junction
                hit: set[int] = set()
                # SHMESH-48l: each trial curve sampled once (a joined web's thousands of curves were sampled again for
                # every shared edge and every ring: most of the time of a mesh)
                smp = {x: _samples(gmsh, x, 4) for x in trial}
                smp_trial = None
                if shared:
                    from scipy.spatial import cKDTree

                    all_pts = np.vstack([smp[x] for x in trial])
                    trial_mass = np.array([float(gmsh.model.occ.getMass(1, x)) for x in trial])
                    for c in shared:
                        tree = cKDTree(_samples(gmsh, c, 256))
                        full = float(gmsh.model.occ.getMass(1, c))
                        far = tree.query(all_pts)[0].reshape(len(trial), -1).max(axis=1)
                        if bool(np.any((far <= 1e-3 * h + full / 256) & (trial_mass < (1 - 1e-6) * full))):
                            hit.add(c)
                # the pieces of the lines inside the face (straight: their two ends), to cut it and its neighbours with
                outline = cKDTree(np.vstack([_samples(gmsh, c, 64) for cl in loops for c in cl])) if hit else None
                pieces: list[tuple[np.ndarray, np.ndarray]] = []
                if hit and outline is not None:
                    for x in trial:
                        if gmsh.model.getType(1, x) != "Line":
                            continue
                        q = smp[x]
                        if outline.query(q[2])[0] > 1e-3 * h:
                            pieces.append((q[0], q[-1]))
                spoilt = False
                if hit and (not CUT_NEIGHBOURS or tag_of[t] in no_cut):
                    # a line would cut a shared edge (the neighbour would keep it whole: a crack) - and cutting the
                    # neighbour is off, or it left poor elements there on a first mesh (SHMESH-47b)
                    small = True
                    spoilt = CUT_NEIGHBOURS
                others = sorted({int(n) for c in hit for n in gmsh.model.getAdjacencies(1, c)[0] if int(n) != t})
                if any(n in by_face or n not in tag_of for n in others) or any(w.face in others for w in washers):
                    small = True        # a neighbour with washers of its own is left as it is
                    causes.append("a line would cut an edge shared with a face with holes of its own")
                elif hit and not spoilt and small and not causes:
                    causes.append("a line would cut a shared edge")
                for w, p in zip(ws, plans, strict=True):
                    if w.corners:
                        continue
                    R = p[2] * w.r if p is not None else w.R
                    # (SHMESH-48l2: only the trial curves within reach of the ring tested one by one, as before)
                    if smp_trial is None:
                        smp_trial = np.stack([smp[x] for x in trial]) if trial else np.zeros((0, 5, 3))
                    d_all = np.linalg.norm(smp_trial - w.centre, axis=2)
                    slack = 1e-9 * R + 1e-12
                    near_r = np.flatnonzero((d_all.min(axis=1) >= R - 1e-4 * R - slack)
                                            & (d_all.max(axis=1) <= R + 1e-4 * R + slack))
                    on_ring = [c for c in (trial[i_] for i_ in near_r.tolist())
                               if np.all(np.abs(np.linalg.norm(smp[c] - w.centre, axis=1) - R) <= 1e-4 * R)]
                    if len(on_ring) != (4 if p is not None else 1):
                        small = True
                        if not any(x.startswith("a ring") for x in causes):
                            c_ = w.centre * mm
                            causes.append(f"a ring came out in {len(on_ring)} pieces, not {4 if p is not None else 1}, "
                                          f"round the {2 * w.r * mm:.3g} mm hole at ({c_[0]:.0f}, {c_[1]:.0f}, "
                                          f"{c_[2]:.0f}) mm")
                if FAST_REMOVE:
                    _remove_cells(gmsh, cells)
                else:
                    gmsh.model.occ.remove([(2, s) for s in cells], recursive=True)
                gmsh.model.occ.synchronize()
                _remove_loose(gmsh, cut_curves)
                # SHMESH-48r: a too-short piece of a drawn line between the outline (or another line) and the line it
                # crosses is dropped (the line ends there in a T, the cells either side become one) and the face tried
                # again; refused only when that does not mend it
                if not (small and TRIM_SHORT and only_small(causes)):
                    break
                if cut_piece is None or cut_piece in trims:
                    break
                trims.append(cut_piece)
            if (small and CELL_HARD is not None and not spoilt and only_small(causes) and along_frame
                    and shortest[0] >= CELL_HARD * CELL_MIN * h):
                # SHMESH-48t: a cell edge under CELL_MIN but over CELL_HARD of it is no reason to refuse the face
                # before it is meshed: it is gridded, and 48f keeps or refuses it on its own elements against the
                # user's lines (the trial floor was stricter than what the face is judged on)
                small = False
            if small:
                # (SHMESH-48r: with what held each line 48p could not move, and the pieces dropped)
                notes_ = [s_ for (ax_, x_), s_ in sorted(pin_notes.items())
                          if any(abs(x * mm - x_) <= 0.01 for x in lines[ax_])]
                if trims:
                    notes_.append(f"48r: {len(trims)} short piece(s) dropped first")
                no("its lines cut a joined face into poor elements on a first mesh" if spoilt
                   else "a cell would come out too small, a ring or a shared edge cut: " + "; ".join(causes + notes_))
                continue
            edge_pts = np.vstack([_samples(gmsh, c, 64) for cl in loops for c in cl])
            if others:
                if cut_out is not None:
                    cut_out.setdefault(tag_of[t], set()).update(tag_of[n] for n in others)
                occ = gmsh.model.occ
                cutters = [(1, occ.addLine(occ.addPoint(*[float(v) for v in pa]), occ.addPoint(*[float(v) for v in pb])))
                           for pa, pb in pieces]
                circles: list[tuple[int, int]] = []
                # (the rings and the cuts from the box corners to the holes are straight lines and circles inside the
                # face: drawn again, with the pieces of the box lines)
                for w, p in zip(ws, plans, strict=True):
                    if w.corners:
                        continue
                    R = p[2] * w.r if p is not None else w.R
                    xa = starts[id(w)] if p is not None else w.xaxis
                    circles.append((1, occ.addCircle(*[float(x) for x in w.centre], float(R),
                                                     zAxis=[float(x) for x in n_face], xAxis=[float(x) for x in xa])))
                _o, omap = occ.fragment([(2, t)] + [(2, n) for n in others], cutters + circles)
                occ.synchronize()
                for j, n in enumerate(others, start=1):
                    kn = tag_of.pop(n)
                    for d, s2 in omap[j]:
                        if d == 2:
                            tag_of[s2] = kn
                omap = [omap[0]] + omap[1 + len(others):]
            else:
                _o, omap = gmsh.model.occ.fragment([(2, t)], tools())
                gmsh.model.occ.synchronize()
            k = tag_of.pop(t)
            new = [s for d, s in omap[0] if d == 2]
            for s in new:
                tag_of[s] = k
            if cells_out is not None:
                cells_out.update(new)
            if made_out is not None:
                made_out.add(k)
            _remove_loose(gmsh, {c for got in omap[1:] for d, c in got if d == 1})
            curves = sorted({abs(int(c)) for s in new for _d, c in gmsh.model.getBoundary([(2, s)], oriented=False)})
            on_circle: set[int] = set()
            # SHMESH-48l2: each cell's curve loops read once (every free box's washer looked through every cell of its
            # face: a column web's holes x cells, hundreds of thousands of reads); nothing below changes the model
            loops_new: dict[int, list] = {}

            def loops_of(s_: int, loops_new=loops_new) -> list:
                if s_ not in loops_new:
                    loops_new[s_] = _curve_loops(gmsh, s_)[1]
                return loops_new[s_]
            for j_, w in enumerate(ws):
                if j_ in fp["in_grid"]:
                    # SHMESH-48k: its ring is cells of the grid: each hole side keeps its count, each line from a
                    # corner to the box is the ring's one row
                    pq = fp["in_grid"][j_]
                    tol = 1e-6 * max(1.0, float(w.R))
                    sides_ = [(o + pq[i_][0] * u + pq[i_][1] * v, o + pq[(i_ + 1) % 4][0] * u + pq[(i_ + 1) % 4][1] * v)
                              for i_ in range(4)]
                    found = 0
                    for c in curves:
                        a_, b_ = _samples(gmsh, c, 1)
                        for i_, (s0, s1) in enumerate(sides_):
                            d_ = s1 - s0
                            on = all(float(np.linalg.norm(s0 + np.clip(float((x - s0) @ d_) / float(d_ @ d_), 0, 1)
                                                          * d_ - x)) <= tol for x in (a_, b_))
                            if on:
                                preset[c] = w.side_counts[i_]
                                found += 1
                                break
                            # the line on past the side's far corner: from that corner to the box
                            if min(float(np.linalg.norm(a_ - s1)), float(np.linalg.norm(b_ - s1))) <= tol and abs(
                                    abs(float((b_ - a_) @ d_)) - float(np.linalg.norm(b_ - a_)) * float(
                                        np.linalg.norm(d_))) <= tol * float(np.linalg.norm(d_)) and float(
                                    np.linalg.norm(b_ - a_)) > tol:
                                preset[c] = w.rows
                                found += 1
                                if cuts is not None:
                                    cuts.add(c)
                                break
                    if found < 8:
                        raise ValueError("a square ring of the grid did not come out as four strips")
                    w.in_grid = True
                    w.info["kind"] = "square ring in a gridded face"
                    continue
                if w.corners:
                    # SHMESH-48h: the square hole now sits in its box's cell: the ring is drawn there (imprint)
                    cell = next(((s, cl) for s in new for cl in [[abs(int(c)) for c in x]
                                                                 for x in loops_of(s)]
                                 if _on_square(gmsh, w, cl)), None)
                    if cell is None:
                        raise ValueError("a square hole was not found in its box")
                    w.face, w.hole, w.in_box = cell[0], cell[1], True
            smp_c = {c: _samples(gmsh, c, 4) for c in curves}       # (sampled once, SHMESH-48l)
            # SHMESH-48l2: the curves' distances to each washer's centre, all at once - only the curves within reach of
            # its hole, ring or box are looked at one by one below (the same test as before, on the same numbers)
            smp_all = np.stack([smp_c[c] for c in curves]) if curves else np.zeros((0, 5, 3))
            for j_w, (w, p) in enumerate(zip(ws, plans, strict=True)):
                if w.corners:
                    continue
                R = p[2] * w.r if p is not None else w.R
                tol = 1e-4 * R
                # SHMESH-48w: with the user's edges, the O-grid's rows from ring to box set where some count fits
                # them (else counted as before, and the delivered rows judged on the mesh)
                k_rb, b_out = None, 0.0
                if p is not None and (low_mm is not None or high_mm is not None):
                    cu_, cv_ = uv(w.centre)
                    bx_ = boxes[j_w]
                    b_in = min(cu_ - bx_[0], bx_[1] - cu_, cv_ - bx_[2], bx_[3] - cv_)
                    b_out = max(math.hypot(x_ - cu_, y_ - cv_) for x_ in bx_[:2] for y_ in bx_[2:])
                    e_hi_ = 0.98 * float(high_mm) / mm if high_mm is not None else math.inf
                    k_rb = _ring_box_rows(R, b_in, b_out, h, EDGE_MARGIN * float(low_mm or 0.0) / mm, e_hi_)
                    if k_rb is None:
                        k_rb = _ring_box_rows(R, b_in, b_out, h, float(low_mm or 0.0) / mm, e_hi_)
                    if k_rb is None and RING_BOX_LEAST and abs(b_out - math.sqrt(2.0) * b_in) <= 0.01 * b_out:
                        # (SHMESH-51o84: none fits with our margins - the count fewest elements put outside the
                        # user's own lines; a square box only: a longer box's long side carries more than its
                        # quarter of the ring, that quarter is no mapped cell, and a count on its cuts mapped it
                        # anyway - top2: 4 triangles in each such quarter, the grid refused, its rings lost)
                        k_rb, over_, under_ = _ring_box_least(
                            R, b_in, b_out, h, float(low_mm or 0.0) / mm,
                            float(high_mm) / mm if high_mm is not None else math.inf, p[0] // 4)
                        w.info["ring_box_rows"] = {"rows": k_rb, "over_longest": over_, "under_shortest": under_}
                ring_c: list[int] = []
                hole_c: list[int] = []
                d_all = np.linalg.norm(smp_all - w.centre, axis=2)
                reach = max(w.r, R, b_out) * (1 + 1e-9) + 1e-12
                lower = min(w.r - CIRCLE_TOL * w.r, w.r - tol, R - tol) - reach * 1e-9
                upper = max(w.r + CIRCLE_TOL * w.r, R + tol, b_out + tol) + reach * 1e-9
                near = np.flatnonzero((d_all.min(axis=1) >= lower) & (d_all.max(axis=1) <= upper))
                for c in (curves[i_] for i_ in near.tolist()):
                    d = np.linalg.norm(smp_c[c] - w.centre, axis=1)
                    if np.all(np.abs(d - w.r) <= CIRCLE_TOL * w.r):
                        hole_c.append(c)
                    elif np.all(np.abs(d - R) <= tol):
                        ring_c.append(c)
                    elif p is not None and d.min() >= w.r - tol and d.max() <= R + tol and abs(d[0] - d[-1]) > 0.5 * (R - w.r):
                        preset[c] = p[1]
                    elif p is not None and k_rb is not None and d.min() >= R - tol and d.max() <= b_out + tol and abs(
                            d[0] - d[-1]) > 0.5 * (b_out - R):
                        # (SHMESH-48w: a cut from the box's corner to the ring - its rows fit the user's edges)
                        q0, q1 = smp_c[c][0], smp_c[c][-1]
                        dr_ = (q1 - q0) / max(float(np.linalg.norm(q1 - q0)), 1e-300)
                        rd_ = (q0 - w.centre) / max(float(np.linalg.norm(q0 - w.centre)), 1e-300)
                        if abs(float(dr_ @ rd_)) > 0.99:
                            preset[c] = k_rb
                on_circle |= set(ring_c) | set(hole_c)
                if p is not None:
                    for c in ring_c:
                        preset[c] = p[0] // 4
                    holes.update(hole_c)
                    w.n, w.rows, w.R = p[0], p[1], R
                    w.out_mm = 2 * math.pi * R * mm / p[0]
                    ogrid.append(w)
                else:
                    if len(ring_c) != 1:
                        raise ValueError("a washer's ring did not come out as one circle")
                    w.ring, w.hole_after = ring_c[0], hole_c
                    for s in new:
                        lp = [sorted(abs(int(c)) for c in cl) for cl in loops_of(s)]
                        if len(lp) == 2 and [w.ring] in lp and _on_hole(gmsh, w, [c for cl in lp for c in cl if c != w.ring]):
                            w.annulus = s
                    if not w.annulus:
                        raise ValueError("a washer's ring did not close round its hole")
                    boxed.append(w)
                    if FREE_BAND and wish_out is not None:
                        _mark_box(gmsh, w, corners_of[j_w], new, h, mm, high_mm, wish_out, loops_of, wish_long)
            if cuts is not None:
                from scipy.spatial import cKDTree

                tree = cKDTree(edge_pts)
                for c in curves:
                    if c in on_circle:
                        continue
                    if tree.query(_samples(gmsh, c, 2)[1])[0] > 1e-3 * h:
                        cuts.add(c)
        except Exception as exc:  # noqa: BLE001
            ogrid[:] = [w for w in ogrid if w not in ws]
            boxed[:] = [w for w in boxed if w not in ws]
            for w in ws:
                w.ring, w.annulus, w.hole_after, w.in_grid = 0, 0, [], False
            no(f"its grid failed ({type(exc).__name__}: {exc})"[:200])
    return ogrid, boxed, preset, holes, why


#: SHMESH-45b: the round free holes of flat faces drawn again as one circle (a CAD circle split into 2 to 5 edges,
#: unevenly, left the washer's hole nodes out of step with its ring's: "a washer's hole and ring nodes do not face each
#: other"), starting on the face frame's diagonal where the face has a straight edge
RESEAT = True

#: SHMESH-45f: four-sided holes take a square ring (off in the guard's round-rings-only step)
SQUARES = True


#: SHMESH-45d: pieces of a hole are one circle when every point of them lies within this share of the radius of one
#: fitted circle (the washer's own round test, _circle_of, is 1e-3 too)
REDRAW_TOL = 1e-3


def _fit_circle_occ(edges: list, normal: np.ndarray) -> tuple[np.ndarray, float, np.ndarray] | None:
    """(centre, radius, axis) of the circle the edges lie on within REDRAW_TOL of its radius, going once round; None
    when they do not (a slot, a polygon, an ellipse)."""
    from OCP.BRepAdaptor import BRepAdaptor_Curve

    pts = []
    for e in edges:
        c = BRepAdaptor_Curve(e)
        for x in np.linspace(c.FirstParameter(), c.LastParameter(), 33):
            p = c.Value(float(x))
            pts.append([p.X(), p.Y(), p.Z()])
    P = np.asarray(pts)
    c0 = P.mean(axis=0)
    _u, _s, vt = np.linalg.svd(P - c0, full_matrices=False)
    n = vt[2] if float(vt[2] @ normal) >= 0 else -vt[2]
    xy = np.column_stack([(P - c0) @ vt[0], (P - c0) @ vt[1]])
    sol, *_r = np.linalg.lstsq(np.column_stack([2 * xy, np.ones(len(xy))]), (xy ** 2).sum(axis=1), rcond=None)
    centre = c0 + sol[0] * vt[0] + sol[1] * vt[1]
    rad = np.linalg.norm(P - centre, axis=1)
    r = float(rad.mean())
    if r <= 0 or float(np.abs(rad - r).max()) > REDRAW_TOL * r or float(np.abs((P - centre) @ n).max()) > REDRAW_TOL * r:
        return None
    ang = np.sort(np.arctan2(xy[:, 1] - sol[1], xy[:, 0] - sol[0]))
    if float(np.diff(np.concatenate([ang, ang[:1] + 2 * math.pi])).max()) > 0.5 * math.pi:
        return None
    return centre, r, n


def _wire_turn(edges: list, centre: np.ndarray, axis: np.ndarray) -> float:
    """The sense a hole's wire goes round its centre about `axis` (+ anticlockwise), as its edges run in the wire."""
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.TopAbs import TopAbs_REVERSED

    total = 0.0
    for e in edges:
        c = BRepAdaptor_Curve(e)
        ts = np.linspace(c.FirstParameter(), c.LastParameter(), 9)
        P = np.array([[c.Value(float(t)).X(), c.Value(float(t)).Y(), c.Value(float(t)).Z()] for t in ts]) - centre
        turn = sum(float(np.cross(a, b) @ axis) for a, b in zip(P[:-1], P[1:], strict=True))
        total += -turn if e.Orientation() == TopAbs_REVERSED else turn
    return total


def reseat_holes(brep, out, only: list | None = None, seam: list | None = None) -> tuple[int, list]:
    """SHMESH-45b: every round hole of a flat face that no other face shares is drawn again as ONE circle starting on the
    diagonal of the face's frame (its longest straight outer edge, as block_faces reads it), the same circle exactly. A
    CAD hole's circle starts at its seam, often in the middle of an O-grid quarter, and the quarter then cannot be
    counted evenly; drawn again, the seam sits where the O-grid cuts anyway. Only the hole's own wire changes (edges
    shared with other faces are untouched, so every junction stays as it is); a face whose area would change is left
    as it was. Writes `out` and returns (how many holes were drawn again - 0: `out` is the input unchanged -, [(each
    one's centre, how many CAD edges it had)]).

    SHMESH-45i/49c: a hole that is one exact circle, or exact halves or quarters, keeps the CAD's edges (its ring is in
    step; moving only the seam changed gmsh's free paving between rings) - unless its centre is in `seam`: the O-grid
    holes of a first mesh, drawn again to start where their O-grid cuts (the face frame's diagonal, or 45 degrees to
    a round plate's radial line)."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeWire
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepTools import BRepTools, BRepTools_ReShape
    from OCP.GCPnts import GCPnts_AbscissaPoint
    from OCP.GeomAbs import GeomAbs_Circle, GeomAbs_Line, GeomAbs_Plane
    from OCP.gp import gp_Ax2, gp_Circ, gp_Dir, gp_Pnt
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED, TopAbs_WIRE
    from OCP.TopExp import TopExp, TopExp_Explorer
    from OCP.TopoDS import TopoDS, TopoDS_Shape
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    from meshpipeline.cad import midsurface as ms

    shape = TopoDS_Shape()
    if not BRepTools.Read_s(shape, str(brep), BRep_Builder()):
        raise ValueError("the mid-surface could not be read")
    emap = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, emap)

    def area(f) -> float:
        g = GProp_GProps()
        BRepGProp.SurfaceProperties_s(f, g)
        return float(g.Mass())

    def edges(w) -> list:
        out_, ex = [], TopExp_Explorer(w, TopAbs_EDGE)
        while ex.More():
            out_.append(TopoDS.Edge_s(ex.Current()))
            ex.Next()
        return out_

    done = 0
    redrawn: list = []
    reshape = BRepTools_ReShape()
    for f in ms._faces(shape):
        sa = BRepAdaptor_Surface(f)
        if sa.GetType() != GeomAbs_Plane:
            continue
        outer = BRepTools.OuterWire_s(f)
        best, u = 0.0, None
        for e in edges(outer):
            c = BRepAdaptor_Curve(e)
            if c.GetType() == GeomAbs_Line:
                ln = float(GCPnts_AbscissaPoint.Length_s(c))
                if ln > best:
                    d = c.Line().Direction()
                    best, u = ln, np.array([d.X(), d.Y(), d.Z()])
        ax = sa.Plane().Axis().Direction()
        nrm = np.array([ax.X(), ax.Y(), ax.Z()])
        diag = None
        if u is not None:
            u = u - float(u @ nrm) * nrm
            u /= max(float(np.linalg.norm(u)), 1e-300)
            diag = (u + np.cross(nrm, u)) / math.sqrt(2.0)
        # SHMESH-49b: on a round plate (its outer edge one circle) each hole starts at 45 degrees to the line from the
        # plate's centre - where polar_faces' O-grid cuts it
        round_c = None
        outer_es = edges(outer)
        if diag is None and len(outer_es) == 1:
            oc = BRepAdaptor_Curve(outer_es[0])
            if oc.GetType() == GeomAbs_Circle:
                lo_ = oc.Circle().Location()
                round_c = np.array([lo_.X(), lo_.Y(), lo_.Z()])
        a0 = area(f)
        ex = TopExp_Explorer(f, TopAbs_WIRE)
        swaps = []
        radii: list[float] = []
        while ex.More():
            w = TopoDS.Wire_s(ex.Current())
            ex.Next()
            if w.IsSame(outer):
                continue
            es = edges(w)
            if not es or any(emap.FindFromKey(e).Size() != 1 for e in es):
                continue
            # SHMESH-45d: one circle within REDRAW_TOL of its radius - pieces that are B-splines or arcs of slightly
            # different centre and radius (a mid-surface's fitted hole) are one circle too
            fit = _fit_circle_occ(es, nrm)
            if fit is None:
                continue
            loc, radius, caxis_fit = fit
            if only is not None and not any(
                    float(np.linalg.norm(np.asarray(c_, dtype=float) - np.asarray(loc, dtype=float))) <= 0.25 * radius
                    for c_ in only):
                continue            # SHMESH-45k: only the holes a first mesh's rings asked for
            circs = [BRepAdaptor_Curve(e) for e in es]
            exact = all(cv.GetType() == GeomAbs_Circle for cv in circs)
            p0 = circs[0].Value(circs[0].FirstParameter())
            rel = np.array([p0.X() - loc[0], p0.Y() - loc[1], p0.Z() - loc[2]])
            rel /= max(float(np.linalg.norm(rel)), 1e-300)
            want = diag
            if want is None and round_c is not None:
                rad = np.asarray(loc, dtype=float) - round_c
                rad = rad - float(rad @ nrm) * nrm
                if float(np.linalg.norm(rad)) > 1e-9 * max(1.0, float(radius)):
                    rad /= float(np.linalg.norm(rad))
                    want = (rad + np.cross(nrm, rad)) / math.sqrt(2.0)
            # (a seam entry is a centre, or (centre, the direction to start at))
            forced = [e for e in (seam or []) if float(np.linalg.norm(
                np.asarray(e[0] if len(e) == 2 else e, dtype=float) - np.asarray(loc, dtype=float))) <= 0.25 * radius]
            force = bool(forced)
            if forced and len(forced[0]) == 2:
                want = np.asarray(forced[0][1], dtype=float)
            spans = [cv.LastParameter() - cv.FirstParameter() for cv in circs] if exact else []
            if not force and exact and len(es) in (2, 4) and max(spans) - min(spans) <= 1e-6 * 2 * math.pi:
                continue            # exact halves or quarters: in step with any ring
            if len(es) == 1 and exact and (not force or want is None or float(rel @ want) > 1 - 1e-9):
                continue            # already one circle (an O-grid's starting where it cuts)
            start = want if want is not None else rel
            caxis = caxis_fit
            if exact:
                cax = circs[0].Circle().Axis().Direction()
                caxis = np.array([cax.X(), cax.Y(), cax.Z()])
            xd = start - float(start @ caxis) * caxis
            circ = gp_Circ(gp_Ax2(gp_Pnt(*[float(x) for x in loc]), gp_Dir(*caxis.tolist()), gp_Dir(*xd.tolist())),
                           float(radius))
            nw = BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(circ).Edge()).Wire()
            if exact:
                if es[0].Orientation() == TopAbs_REVERSED:
                    nw.Reverse()
            elif _wire_turn(es, loc, caxis) < 0:
                nw.Reverse()
            swaps.append((w, nw))
            radii.append(float(radius))
            redrawn.append(([float(x) for x in loc], len(es)))
        if not swaps:
            continue
        trial = BRepTools_ReShape()
        for w, nw in swaps:
            trial.Replace(w, nw)
        nf = TopoDS.Face_s(trial.Apply(f))
        # the face's area kept: to a millionth, or to what the holes' own fitting tolerance can move it
        give = sum(2 * math.pi * float(r_) * REDRAW_TOL * float(r_) for r_ in radii)
        if abs(area(nf) - a0) > 1e-6 * max(a0, 1e-300) + give:
            continue
        for w, nw in swaps:
            reshape.Replace(w, nw)
        done += len(swaps)
    if not done:
        BRepTools.Write_s(shape, str(out))
        return 0, []
    result = reshape.Apply(shape)
    if len(ms._faces(result)) != len(ms._faces(shape)):
        BRepTools.Write_s(shape, str(out))
        return 0, []
    BRepTools.Write_s(result, str(out))
    return done, redrawn


#: SHMESH-49: a round flat plate with its holes on one bolt circle is cut into rings and spokes: two circles round the
#: bolt circle (BOX_FREE face sizes clear of the washers' rings), a spoke each side of every hole and one in the middle
#: of a gap wider than POLAR_GAP face sizes, the middle of a full disc left whole (paved: a mapped core square would take
#: a quarter circle's count on a side 0.45 times as long). The sectors are four-sided (mapped); each washer keeps its ring in its own
#: sector, paved from ring to sector. Holes on one circle within POLAR_ROUND of its radius, spaced alike within
#: POLAR_EVEN times.
POLAR = True
#: SHMESH-49g: a round plate whose rings and strips leave triangles or poor elements is meshed again without them
POLAR_CHECK = True
POLAR_TRIAS = 4
#: SHMESH-49g: ... or this share of its elements, when every one meets the tria angle lines
POLAR_TRIA_SHARE = 0.01
#: SHMESH-49f: each hole's box square (straight across its strip) or bounded by the bolt circle's rings (arcs)
POLAR_SQUARE = True
#: SHMESH-49h: each box side bowed out by this many degrees at its ends: a square box's corner splits into two 45 degree
#: O-grid corners, on the quad-angle line (split into triangles by the clean-up); bowed, 45 plus this
POLAR_BOW = 12.0
#: SHMESH-49i: spokes run on inside the bolt circle's inner ring to a central hole (off: that band is one paved ring - a
#: count running from the central hole to the rim could not keep both its ends inside the user's edges)
POLAR_INNER_SPOKES = False
POLAR_ROUND = 0.02
POLAR_EVEN = 1.5
POLAR_GAP = 3.0


def _round_loop(gmsh, curves: list[int]) -> tuple[np.ndarray, float] | None:
    """(centre, radius) when a loop is one circle (in any number of arcs), else None."""
    got = _circle_of(gmsh, curves)
    return (got[0], got[1]) if got is not None else None


@dataclass
class Band:
    """SHMESH-49l: the band of a round plate between its central opening and its bolt circle's inner ring."""
    face: int                       # the cell (gmsh surface)
    owner: int                      # the sewn face
    centre: np.ndarray
    normal: np.ndarray
    inner: list[int]                # the opening's curves
    outer: list[int]                # the ring's pieces (arcs between strips, box sides across them)
    r_in: float
    n_in: int = 0


#: SHMESH-49l: the band inside a bolt circle (round a central opening) meshed in rings of quads, the count dropping
#: from the ring's to the opening's in one row of 3-to-1 groups (none: paved by gmsh). Off: the 3-to-1 quads come out
#: with a taper of 0.50-0.57 against Nastran's 0.5 and the clean-up splits them (60-104 triangles on a look-alike plate)
POLAR_BAND = False
#: SHMESH-51o10: ... but where the opening can take the ring's own count (its segments no shorter than BAND_EQUAL_MIN
#: of the plate's size), the band is meshed in rings of that count, no drop and no 3-to-1 group (a real round plate's
#: band round a 325 mm bore was paved, 17 to 23 triangles)
POLAR_BAND_EQUAL = True
BAND_EQUAL_MIN = 0.5
BAND_SMOOTH = 5
BAND_AB_DEPTH = 0.65
BAND_AB_SPREAD = 0.25


def band_counts(gmsh, band: Band, known: dict[int, int], size_mm: float, mm: float) -> int | None:
    """SHMESH-49l: the count round the band's opening (set on its curves), the ring's count less two for each 3-to-1
    group so the opening's elements come out near the ring's in length; None when the ring is not all counted."""
    if any(c not in known for c in band.outer):
        return None
    n_out = sum(known[c] for c in band.outer)
    lengths_in = [float(gmsh.model.occ.getMass(1, c)) for c in band.inner]
    if POLAR_BAND_EQUAL and sum(lengths_in) * mm / max(n_out, 1) >= BAND_EQUAL_MIN * size_mm:
        # SHMESH-51o10: the ring's own count on the opening - rings of quads, no 3-to-1 group
        n_in = n_out
    elif not POLAR_BAND:
        return None
    else:
        r_out = float(np.mean([np.linalg.norm(_samples(gmsh, c, 4) - band.centre, axis=1).mean() for c in band.outer]))
        want = n_out * band.r_in / max(r_out, 1e-300)
        n_in = n_out - 2 * max(0, round((n_out - want) / 2))
        n_in = max(n_in, n_out - 2 * (n_out // 3), 8)
        if (n_out - n_in) % 2:
            n_in -= 1
    lengths = [float(gmsh.model.occ.getMass(1, c)) for c in band.inner]
    spread = _spread(n_in, lengths) if len(band.inner) > 1 else [n_in]
    if spread is None:
        return None
    for c, m in zip(band.inner, spread, strict=True):
        gmsh.model.mesh.setTransfiniteCurve(c, m + 1)
    band.n_in = n_in
    return n_in


def band_mesh(gmsh, band: Band, index: np.ndarray, xyz: np.ndarray, mm: float, h_mm: float
              ) -> tuple[np.ndarray, np.ndarray]:
    """SHMESH-49l: the band's own quads - rings from the bolt circle's inner ring in to the opening, one row of 3-to-1
    groups (four quads round two inner nodes each) spread round it. Returns (new points, quads in xyz indices: the
    new points numbered after xyz). ValueError when the counts or the quads do not come out."""
    def ring_nodes(curves: list[int]) -> np.ndarray:
        got: set[int] = set()
        for c in curves:
            tags, _x, _p = gmsh.model.mesh.getNodes(1, c, includeBoundary=True)
            got |= set(index[np.asarray(tags, dtype=np.int64)].tolist())
        return np.array(sorted(got), dtype=np.int64)

    C = band.centre * mm
    n = band.normal / max(float(np.linalg.norm(band.normal)), 1e-300)
    outer, inner = ring_nodes(band.outer), ring_nodes(band.inner)
    if len(inner) != band.n_in or len(outer) < len(inner) or (len(outer) - len(inner)) % 2:
        raise ValueError(f"the band's rings carry {len(outer)} and {len(inner)} nodes")
    e1 = xyz[outer[0]] - C
    e1 = e1 - float(e1 @ n) * n
    e1 /= max(float(np.linalg.norm(e1)), 1e-300)
    e2 = np.cross(n, e1)

    def polar(ids: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        d = xyz[ids] - C
        a = np.arctan2(d @ e2, d @ e1) % (2 * math.pi)
        o = np.argsort(a)
        return ids[o], a[o], np.linalg.norm(d[o] - np.outer(d[o] @ n, n), axis=1)

    outer, a_out, r_out = polar(outer)
    inner, a_in, _r_in = polar(inner)
    n_out, n_in = len(outer), len(inner)
    groups = (n_out - n_in) // 2
    # the outer segments that start a 3-to-1 group: spread evenly round the ring
    starts = sorted({int(round(g * n_out / groups)) % n_out for g in range(groups)}) if groups else []
    if len(starts) != groups or any((b - a) % n_out < 3 for a, b in zip(starts, starts[1:] + starts[:1], strict=True)
                                    if groups > 1):
        raise ValueError("the band's 3-to-1 groups do not fit round its ring")
    rows = max(2, int(round(float(np.mean(r_out) - band.r_in * mm) / h_mm)))
    t_row = rows // 2                   # the transition between ring t_row and ring t_row + 1
    # the angles each ring's nodes sit at: the ring's own above the transition, the kept ones below it, eased to the
    # opening's own at the opening
    kept = [j for j in range(n_out) if not any((j - s0) % n_out in (1, 2) for s0 in starts)]
    a_kept = a_out[kept]
    shift = int(np.argmin(np.abs(((a_in - a_kept[0] + math.pi) % (2 * math.pi)) - math.pi)))
    inner, a_in = np.roll(inner, -shift), np.roll(a_in, -shift)
    a_in_u = a_kept + ((a_in - a_kept + math.pi) % (2 * math.pi) - math.pi)
    pts: list[np.ndarray] = []
    base = len(xyz)

    def at(radius: np.ndarray, ang: np.ndarray) -> np.ndarray:
        return C + radius[:, None] * (np.cos(ang)[:, None] * e1 + np.sin(ang)[:, None] * e2)

    def add(p_: np.ndarray) -> np.ndarray:
        start = base + sum(len(x) for x in pts)
        pts.append(p_)
        return np.arange(start, start + len(p_))

    r_in = band.r_in * mm
    rings: list[np.ndarray] = [outer]
    for i in range(1, t_row + 1):
        f = i / rows
        rings.append(add(at((1 - f) * r_out + f * r_in, a_out)))
    depth = (t_row + BAND_AB_DEPTH) / rows
    mid_r = (1 - depth) * r_out + depth * r_in
    ab = {}
    for s0 in starts:
        # the group's two inner nodes spread over its span and set low in the row (top side to bottom about 1.5 to
        # 3: a middle quad at the outer nodes' own angles came out tapered past the solver's limits)
        j0, j1, j2, j3 = ((s0 + q) % n_out for q in range(4))
        span = (a_out[j3] - a_out[j0]) % (2 * math.pi)
        aa = np.array([a_out[j0] + BAND_AB_SPREAD * span, a_out[j0] + (1 - BAND_AB_SPREAD) * span])
        ab[s0] = add(at(np.array([mid_r[j1], mid_r[j2]]), aa))
    lower: list[np.ndarray] = []
    for i in range(t_row + 1, rows):
        f = (i - t_row - 1) / max(rows - t_row - 1, 1)
        ang = (1 - f) * a_kept + f * a_in_u
        r_here = (1 - i / rows) * r_out[kept] + i / rows * r_in
        lower.append(add(at(r_here, ang)))
    lower.append(inner)
    quads: list[list[int]] = []
    for i in range(t_row):
        up, dn = rings[i], rings[i + 1]
        for j in range(n_out):
            quads.append([up[j], up[(j + 1) % n_out], dn[(j + 1) % n_out], dn[j]])
    up, dn = rings[t_row], lower[0]
    pos = {j: q for q, j in enumerate(kept)}
    j = 0
    while j < n_out:
        if j in ab:
            a_, b_ = ab[j]
            o0, o1, o2, o3 = (up[(j + q) % n_out] for q in range(4))
            i0, i1 = dn[pos[j]], dn[pos[(j + 3) % n_out]]
            quads += [[o0, o1, a_, i0], [o1, o2, b_, a_], [o2, o3, i1, b_], [a_, b_, i1, i0]]
            j += 3
        else:
            quads.append([up[j], up[(j + 1) % n_out], dn[pos[(j + 1) % n_out]], dn[pos[j]]])
            j += 1
    for i in range(len(lower) - 1):
        up, dn = lower[i], lower[i + 1]
        for q in range(n_in):
            quads.append([up[q], up[(q + 1) % n_in], dn[(q + 1) % n_in], dn[q]])
    Q = np.array(quads, dtype=np.int64)
    P = np.vstack(pts) if pts else np.zeros((0, 3))
    allx = np.vstack([xyz, P])
    # a light smoothing of the band's own new nodes (each to the mean of its neighbours, in the plate's plane)
    nb: dict[int, set[int]] = {}
    for q in Q:
        for k_ in range(4):
            nb.setdefault(int(q[k_]), set()).update((int(q[k_ - 1]), int(q[(k_ + 1) % 4])))
    free = [v for v in nb if v >= base]
    for _ in range(BAND_SMOOTH):
        for v in free:
            m_ = allx[list(nb[v])].mean(axis=0)
            allx[v] = m_ - float((m_ - C) @ n) * n
    # turned as the plate's normal
    nq = np.cross(allx[Q[:, 1]] - allx[Q[:, 0]], allx[Q[:, 3]] - allx[Q[:, 0]])
    flip = nq @ n < 0
    Q[flip] = Q[flip][:, ::-1]
    return allx[base:], Q


def polar_faces(gmsh, washers: list[Washer], tag_of: dict[int, int], size_mm: dict[int, float], mm: float,
                cuts: set[int] | None = None, cells_out: set[int] | None = None, hole_size=None,
                low_mm: float | None = None, high_mm: float | None = None, no_polar: frozenset = frozenset(),
                made_out: set[int] | None = None, bands_out: list | None = None,
                why_where: dict[str, list[int]] | None = None
                ) -> tuple[list[Washer], dict[str, int], list[Washer], dict[int, int], set[int]]:
    """SHMESH-49: the round flat faces whose holes all have washers on one bolt circle, cut into rings and spokes (see
    POLAR). A face sharing an edge a line would end on, or whose cells would come out under CELL_MIN face sizes, or
    whose rings would be cut, is left as it is (tried on a copy first). Returns (the washers so made - each with its
    ring and annulus drawn, in a free sector -, {why a round face was not cut: count}, the O-grid washers, {curve:
    count} their grid must keep, their holes' curves). SHMESH-49b: where every hole takes the same O-grid plan
    (_boxed, inside the user's edges), each sector is that plan's box and its four corners are joined to the ring and
    the hole, so the sector is four mapped cells and its annulus four more - no free paving round a hole."""
    by_face: dict[int, list[Washer]] = {}
    for w in washers:
        by_face.setdefault(w.face, []).append(w)
    made: list[Washer] = []
    ogrid: list[Washer] = []
    preset: dict[int, int] = {}
    holes: set[int] = set()
    why: dict[str, int] = {}

    def no(reason: str) -> None:
        why[reason] = why.get(reason, 0) + 1
        if why_where is not None:
            # the record says which face each reason came from (SHMESH-48f)
            why_where.setdefault(reason, []).append(int(tag_of.get(t, -1)))

    occ = gmsh.model.occ
    for t, ws in sorted(by_face.items()):
        _l, loops = _curve_loops(gmsh, t)
        loops = [[abs(int(c)) for c in cl] for cl in loops]
        hole_loops = [sorted(w.hole) for w in ws]
        rest = [cl for cl in loops if sorted(cl) not in hole_loops]
        length = [sum(float(occ.getMass(1, c)) for c in cl) for cl in rest]
        if not rest:
            continue
        outer = rest[int(np.argmax(length))]
        rim = _round_loop(gmsh, outer)
        if rim is None or len(ws) < 3:
            continue                                    # not a round plate: the rectangular grid's (block_faces)
        if tag_of.get(t) in no_polar:
            # SHMESH-49g: its rings and strips left more than POLAR_TRIAS triangles on a first mesh
            no("its rings and strips left triangles on a first mesh")
            continue
        C, Ro = rim
        inner = [cl for cl in rest if cl is not outer]
        Ri = 0.0
        inner_shared = False
        if inner:
            core = _round_loop(gmsh, inner[0]) if len(inner) == 1 else None
            if core is None or float(np.linalg.norm(core[0] - C)) > 1e-3 * Ro:
                no("a round plate with another hole not on its centre")
                continue
            Ri = core[1]
            # (a central circle another face shares - a tube welded on - stays whole: the band inside the bolt
            # circle's inner ring is then left one annulus, paved)
            inner_shared = any(len(gmsh.model.getAdjacencies(1, c)[0]) > 1 for c in inner[0])
        h = size_mm[t] / mm
        n_face = ws[0].normal
        e1 = ws[0].centre - C
        e1 = e1 - float(e1 @ n_face) * n_face
        e1 /= max(float(np.linalg.norm(e1)), 1e-300)
        e2 = np.cross(n_face, e1)
        rb = [float(np.linalg.norm(w.centre - C)) for w in ws]
        Rb = float(np.mean(rb))
        if max(abs(x - Rb) for x in rb) > POLAR_ROUND * Rb:
            no("the holes are not on one bolt circle")
            continue
        ang = sorted((math.atan2(float((w.centre - C) @ e2), float((w.centre - C) @ e1)) % (2 * math.pi), w)
                     for w in ws)
        gaps = [((ang[(i + 1) % len(ang)][0] - ang[i][0]) % (2 * math.pi)) for i in range(len(ang))]
        if max(gaps) > POLAR_EVEN * min(gaps):
            no("the holes are not spaced alike round their circle")
            continue
        # one plan for holes of one size (a hole drawn again comes out a hair off its radius: one plan each split the
        # bolt circle between an O-grid and none)
        radii = [w.r for w in ws]
        plans: list[tuple[int, int, float, float] | None]
        if hole_size is None:
            plans = [None] * len(ws)
        elif max(radii) <= 1.01 * min(radii):
            r_m = float(np.mean(radii))
            one = _boxed(r_m * mm, hole_size(r_m * mm, ws[0].owner), size_mm[t], low_mm, high_mm)
            plans = [one] * len(ws)
        else:
            plans = [_boxed(w.r * mm, hole_size(w.r * mm, w.owner), size_mm[t], low_mm, high_mm) for w in ws]
        got_plans = [pl for pl in plans if pl is not None]
        use_o = len(got_plans) == len(ws) and len({pl[0] for pl in got_plans}) == 1 and \
            len({pl[1] for pl in got_plans}) == 1
        if not use_o:
            # SHMESH-49d: no O-grid fits the holes inside the user's edges - a ring in a paved sector put a ring of
            # triangles round every hole (16 a hole on a real base plate): the plate keeps its free-face washers
            no("no O-grid fits its holes inside the user's edges")
            continue
        plan_of: dict[int, tuple[int, int, float, float]] = {}
        if use_o:
            b = max(pl[3] for pl in got_plans) / mm
            R_of = {id(w): pl[2] * w.r for w, pl in zip(ws, got_plans, strict=True)}
            plan_of = {id(w): pl for w, pl in zip(ws, got_plans, strict=True)}
        else:
            b = max(w.R for w in ws) + BOX_FREE * h
            R_of = {id(w): w.R for w in ws}
        beta = math.asin(min(0.99, b / Rb))
        if min(gaps) - 2 * beta < CELL_MIN * h / Rb:
            no("the washers' sectors would touch")
            continue
        # SHMESH-49f: each hole's box is square - across its strip the rings are straight (chords b from the hole's
        # centre), so the box's corners are square and its O-grid diagonals at 45 degrees (an arc of the ring there
        # met the strip's side at 77 degrees: O-grid cells with 38 degree corners, left unmapped); r1 and r2 are the
        # radii the chords end on
        if POLAR_SQUARE:
            r1 = math.sqrt((Rb - b) ** 2 + b * b)
            r2 = math.sqrt((Rb + b) ** 2 + b * b)
        else:
            r1, r2 = Rb - b, Rb + b
        if Ro - r2 < SNAP * h:
            r2 = Ro
        if Ri > 0 and r1 - Ri < SNAP * h:
            r1 = Ri
        if Ri == 0 and r1 < 2 * h:
            no("no room for a core inside the bolt circle")
            continue
        if r2 > Ro or r1 < Ri:
            no("the washers' sectors run past the plate")
            continue
        start = Ri if Ri > 0 and not inner_shared and POLAR_INNER_SPOKES else r1
        # SHMESH-49b: a washer's strip is bounded by two lines parallel to its radial line, b each side, from the
        # bolt circle's inner ring out to the rim: as wide (2b) at the rim as at its box, so its quarter count fits all
        # the way along (radial spokes fanned out: the arcs at the rim came out 1.2 times the box). SHMESH-49d: inside
        # the inner ring (to a central hole) the strip's sides turn radial - parallel sides of neighbouring strips
        # met there in thin wedges, paved with triangles
        if 2 * math.asin(min(0.99, b / r1)) > min(gaps) - CELL_MIN * h / r1:
            no("the washers' strips would touch")
            continue
        if use_o:
            # the strip's count runs on to the rim and the core: sized by its longest arc, checked against the hole
            n0, rows0, ratio0, b0 = next(iter(plan_of.values()))
            # (an arc is a little longer than the box's chord: 5% over our own bound, never past the user's edge)
            seg_hi = 1.05 * BOX_SEG[1] * size_mm[t] if high_mm is None else min(1.05 * BOX_SEG[1] * size_mm[t],
                                                                                  0.98 * high_mm)
            arc = 2 * r1 * math.asin(min(1.0, b / r1))
            q = max(n0 // 4, math.ceil(arc * mm / seg_hi - 1e-9))
            # (and inside the inner ring, where the sides turn radial, the arc at the central hole stays over the
            # user's shortest edge)
            inner_arc = 2 * start * math.asin(min(1.0, b / r1)) * mm / q
            if q > n0 // 4 or inner_arc < float(low_mm or 0.0):
                if min(2 * math.pi * w.r * mm / (4 * q) for w in ws) < float(low_mm or 0.0) or 4 * q > WASHER_MAX \
                        or inner_arc < float(low_mm or 0.0):
                    no("a strip's elements would come out past the user's edges from the hole to the rim")
                    continue
                plan_of = dict.fromkeys(plan_of, (4 * q, rows0, ratio0, b0))
        spokes: list[tuple[float, float]] = []      # (angle, offset): a line parallel to that radial line
        for i, (a, _w) in enumerate(ang):
            spokes += [(a, -b), (a, b)]
            if gaps[i] * Rb > (POLAR_GAP + 2 * b / h) * h:
                spokes.append((a + gaps[i] / 2, 0.0))

        def on(r: float, a: float, off: float, C=C, e1=e1, e2=e2) -> np.ndarray:
            """The point at radius r on the line parallel to angle a's radial line, `off` to its side."""
            u_ = math.cos(a) * e1 + math.sin(a) * e2
            v_ = -math.sin(a) * e1 + math.cos(a) * e2
            return C + math.sqrt(max(r * r - off * off, 0.0)) * u_ + off * v_

        def sector_corners(a: float, r1=r1, r2=r2, b=b, on=on) -> list[np.ndarray]:
            return [on(r, a, sg * b) for r, sg in ((r1, -1), (r2, -1), (r2, 1), (r1, 1))]

        def tools(C=C, Ri=Ri, Ro=Ro, ang=ang, n_face=n_face, r1=r1, r2=r2, spokes=spokes, start=start, on=on,
                  use_o=use_o, R_of=R_of, sector_corners=sector_corners, e1=e1, e2=e2) -> list[tuple[int, int]]:
            out = []
            centre_pt = occ.addPoint(*[float(x) for x in C])
            centre_of = {a_: w_.centre for a_, w_ in ang}

            def bowed(t0: int, t1: int, p0: np.ndarray, p1: np.ndarray, hc: np.ndarray) -> tuple[int, int]:
                """SHMESH-49h: a box side from p0 to p1, bowed away from its hole's centre hc by POLAR_BOW."""
                mid = 0.5 * (p0 + p1)
                away = mid - hc
                away = away - float(away @ n_face) * n_face
                away /= max(float(np.linalg.norm(away)), 1e-300)
                sag = 0.5 * float(np.linalg.norm(p1 - p0)) * math.tan(math.radians(POLAR_BOW) / 2)
                if sag <= 0:
                    return (1, occ.addLine(t0, t1))
                return (1, occ.addCircleArc(t0, occ.addPoint(*[float(x) for x in mid + sag * away]), t1, center=False))
            for r in sorted({r1, r2} - {Ri, Ro, 0.0}):
                # the ring at r: straight across each strip (its box's square side), an arc between strips
                on_r = sorted(((math.atan2(float((on(r, a_, off) - C) @ e2), float((on(r, a_, off) - C) @ e1))
                                % (2 * math.pi), a_, off) for a_, off in spokes))
                tags = [occ.addPoint(*[float(x) for x in on(r, a_, off)]) for _ang, a_, off in on_r]
                for j in range(len(on_r)):
                    k_ = (j + 1) % len(on_r)
                    same_strip = POLAR_SQUARE and on_r[j][1] == on_r[k_][1] and on_r[j][2] < 0 < on_r[k_][2]
                    if same_strip:
                        out.append(bowed(tags[j], tags[k_], on(r, on_r[j][1], on_r[j][2]),
                                         on(r, on_r[k_][1], on_r[k_][2]), centre_of[on_r[j][1]]))
                    else:
                        out.append((1, occ.addCircleArc(tags[j], centre_pt, tags[k_])))
            for a, off in spokes:
                bend = on(r1, a, off)
                if off != 0.0 and POLAR_SQUARE and POLAR_BOW > 0 and r2 < Ro - 1e-9:
                    # a box's side (r1 to r2 along its strip) bowed out, then straight on to the rim
                    bend2 = on(r2, a, off)
                    out.append(bowed(occ.addPoint(*[float(x) for x in bend]), occ.addPoint(*[float(x) for x in bend2]),
                                     bend, bend2, centre_of[a]))
                    if r2 < Ro - 1e-9:
                        out.append((1, occ.addLine(occ.addPoint(*[float(x) for x in bend2]),
                                                   occ.addPoint(*[float(x) for x in on(Ro * 1.02, a, off)]))))
                else:
                    out.append((1, occ.addLine(occ.addPoint(*[float(x) for x in bend]),
                                               occ.addPoint(*[float(x) for x in on(Ro * 1.02, a, off)]))))
                if start < r1 - 1e-9:
                    # inside the inner ring: radial, through the bend, in to the central hole
                    u_ = (bend - C) / max(float(np.linalg.norm(bend - C)), 1e-300)
                    out.append((1, occ.addLine(occ.addPoint(*[float(x) for x in C + 0.98 * start * u_]),
                                               occ.addPoint(*[float(x) for x in bend]))))
            for a, w in ang:
                R = R_of[id(w)]
                if not use_o:
                    out.append((1, occ.addCircle(*[float(x) for x in w.centre], float(R),
                                                 zAxis=[float(x) for x in n_face], xAxis=[float(x) for x in w.xaxis])))
                    continue
                cs = sector_corners(a)
                # SHMESH-49e: the ring's and hole's points toward the strip box's own corners (the box is longer than
                # wide where the ring sits off the bolt circle's middle: points at 45 degrees left cells with 38 degree
                # corners, past what is mapped); the redraw starts the hole at the first of them
                dgs = []
                for cp in cs:
                    d_ = cp - w.centre
                    d_ = d_ - float(d_ @ n_face) * n_face
                    dgs.append(d_ / max(float(np.linalg.norm(d_)), 1e-300))
                w.seam = [float(x) for x in dgs[0]]
                # the ring's seam on a corner's diagonal: no seam inside a quarter
                out.append((1, occ.addCircle(*[float(x) for x in w.centre], float(R),
                                             zAxis=[float(x) for x in n_face], xAxis=[float(x) for x in dgs[0]])))
                for cp, dg in zip(cs, dgs, strict=True):
                    q1 = occ.addPoint(*[float(x) for x in w.centre + R * dg])
                    out.append((1, occ.addLine(occ.addPoint(*[float(x) for x in cp]), q1)))
                    out.append((1, occ.addLine(q1, occ.addPoint(*[float(x) for x in w.centre + 0.5 * w.r * dg]))))
            return out

        # tried on a copy first: cells no smaller than CELL_MIN face sizes, every ring one circle, no shared edge cut
        shared = [c for cl in loops for c in cl if len(gmsh.model.getAdjacencies(1, c)[0]) > 1]
        copy = occ.copy([(2, t)])
        _o, cmap = occ.fragment(copy, tools())
        occ.synchronize()
        cells = [s for d, s in cmap[0] if d == 2]
        cut_curves = {c for got in cmap[1:] for d, c in got if d == 1}
        trial = sorted({abs(int(c)) for s in cells for _d, c in gmsh.model.getBoundary([(2, s)], oriented=False)})
        # (a hole's own arcs aside: its circle's seam may split a quarter - a chain, mapped all the same)
        on_holes = {c for c in trial for w in ws
                    if np.all(np.abs(np.linalg.norm(_samples(gmsh, c, 4) - w.centre, axis=1) - w.r) <= CIRCLE_TOL * w.r)}
        bad = any(float(occ.getMass(1, c)) < CELL_MIN * h for c in trial if c not in on_holes)
        for _a, w in ang:
            R = R_of[id(w)]
            on_ring = [c for c in trial if np.all(np.abs(np.linalg.norm(_samples(gmsh, c, 4) - w.centre, axis=1) - R)
                                                  <= 1e-4 * R)]
            bad = bad or len(on_ring) != (4 if use_o else 1)
        if shared:
            from scipy.spatial import cKDTree

            for c in shared:
                tree = cKDTree(_samples(gmsh, c, 256))
                full = float(occ.getMass(1, c))
                for x in trial:
                    if tree.query(_samples(gmsh, x, 4))[0].max() <= 1e-3 * h + full / 256 and \
                            float(occ.getMass(1, x)) < (1 - 1e-6) * full:
                        bad = True
        if FAST_REMOVE:
            _remove_cells(gmsh, cells)
        else:
            occ.remove([(2, s) for s in cells], recursive=True)
        occ.synchronize()
        _remove_loose(gmsh, cut_curves)
        if bad:
            no("a sector would come out too small, a ring or a shared edge cut")
            continue
        edge_pts = np.vstack([_samples(gmsh, c, 64) for cl in loops for c in cl])
        _o, omap = occ.fragment([(2, t)], tools())
        occ.synchronize()
        k = tag_of.pop(t)
        if made_out is not None:
            made_out.add(k)
        new = [s for d, s in omap[0] if d == 2]
        for s in new:
            tag_of[s] = k
        if cells_out is not None:
            cells_out.update(new)
        _remove_loose(gmsh, {c for got in omap[1:] for d, c in got if d == 1})
        curves = sorted({abs(int(c)) for s in new for _d, c in gmsh.model.getBoundary([(2, s)], oriented=False)})
        if bands_out is not None and (POLAR_BAND or POLAR_BAND_EQUAL) and Ri > 0 and not inner_shared and start >= r1 - 1e-9:
            # SHMESH-49l: the band round the central opening, meshed in rings (band_mesh) once its ring is counted
            for s_ in new:
                bl = [abs(int(c)) for _d, c in gmsh.model.getBoundary([(2, s_)], oriented=False)]
                on_in = [c for c in bl if np.all(np.abs(np.linalg.norm(_samples(gmsh, c, 4) - C, axis=1) - Ri)
                                                  <= 1e-4 * Ri)]
                if on_in and len(_curve_loops(gmsh, s_)[1]) == 2:
                    bands_out.append(Band(face=s_, owner=k, centre=C, normal=n_face, inner=on_in,
                                          outer=[c for c in bl if c not in on_in], r_in=Ri))
        on_circle: set[int] = set()
        for _a, w in ang:
            R = R_of[id(w)]
            tol = 1e-4 * R
            ring_c, hole_c = [], []
            for c in curves:
                d = np.linalg.norm(_samples(gmsh, c, 4) - w.centre, axis=1)
                if np.all(np.abs(d - w.r) <= CIRCLE_TOL * w.r):
                    hole_c.append(c)
                elif np.all(np.abs(d - R) <= tol):
                    ring_c.append(c)
                elif use_o and d.min() >= w.r - tol and d.max() <= R + tol and abs(d[0] - d[-1]) > 0.5 * (R - w.r):
                    preset[c] = plan_of[id(w)][1]                    # a row line from the hole to the ring
            on_circle |= set(ring_c) | set(hole_c)
            if use_o:
                pl = plan_of[id(w)]
                for c in ring_c:
                    preset[c] = pl[0] // 4
                holes.update(hole_c)
                w.n, w.rows, w.R = pl[0], pl[1], R
                w.out_mm = 2 * math.pi * R * mm / pl[0]
                w.info["kind"] = "o-grid in a sector of a round plate"
                ogrid.append(w)
                continue
            w.ring, w.hole_after = ring_c[0], hole_c
            for s in new:
                _l3, lp = _curve_loops(gmsh, s)
                lp = [sorted(abs(int(c)) for c in cl) for cl in lp]
                if len(lp) == 2 and [w.ring] in lp and _on_hole(gmsh, w, [c for cl in lp for c in cl if c != w.ring]):
                    w.annulus = s
            if not w.annulus:
                raise ValueError("a washer's ring did not close round its hole")
            w.info["kind"] = "ring in a sector of a round plate"
            made.append(w)
        if cuts is not None:
            from scipy.spatial import cKDTree

            tree = cKDTree(edge_pts)
            for c in curves:
                if c not in on_circle and tree.query(_samples(gmsh, c, 2)[1])[0] > 1e-3 * h:
                    cuts.add(c)
    return made, why, ogrid, preset, holes


__all__ = ["WASHERS", "MAPPED", "Washer", "plan_washers", "imprint", "set_counts", "ring_mesh", "map_faces", "block_faces", "reseat_holes",
           "polar_faces"]

