# Responsibility: Read from the part's whole skin what the scout's face-by-face reading cannot see:
# whether the shape is a body in a flow or a passage (its symmetry, how convex it is, how thick an
# annular end is), which way the flow goes past a body (a rotor's axis, a body of revolution's
# axis, a mirror plane, a half model's cut face, a straight bar's span), and which opening is the
# inlet where a diffuser says so - each with how sure it is and why, in plain words.
# Boundaries: pure numpy/scipy over triangles in metres. No file format, no model call, no
# persistence. It proposes; the user (and the pictures) confirm. A shape it cannot read says so.
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

#: Surface points sampled per part, by area: enough that a blade a fiftieth of the part across
#: carries hundreds of them, few enough that every symmetry test is a fraction of a second.
SAMPLES = 20_000
#: Past this many triangles the shape is not read at all (cad/up_axis's bound): the reading is
#: optional and must never be what makes a big upload's check late.
MAX_TRIANGLES = 1_000_000
#: A symmetry HOLDS when this share of the part's surface, moved by it, lands back on the surface
#: (see _lands). A car mirrored left to right lands 100%; the Ahmed body mirrored front to back
#: lands 74%, a rotor turned by the wrong share of a circle about half.
MATCH = 0.97
#: Each symmetry is screened on this many samples first, and dropped below SCREEN_MATCH.
SCREEN, SCREEN_MATCH = 2_000, 0.9
#: Rotations tried about each axis: 2..12-fold. A turn by an odd share of a circle tells a body
#: of revolution (it lands on itself) from a bladed rotor (it does not).
MAX_ORDER = 12
ODD_TURNS = (0.137, 0.291)
#: An EXTRUSION (a straight bar, an extruded wing section) has all of its surface either square to
#: its axis (the ends) or along it (the sides).
EXTRUDED = 0.97
#: A flat on the end of the part's box is a CUT FACE (a half model's symmetry plane, a wing's
#: root) when it covers this much of that side of the box and the opposite end has nothing like
#: it (under OTHER_END of its area).
CUT_FACE = 0.25
OTHER_END = 0.5
#: A body this thin against its middle side is a lifting surface or a plate (a wing is a tenth).
THIN_BODY = 0.2
#: A ROTOR - blades round a hub - fills little of its box; an annular passage with blades cut out
#: of it fills half.
ROTOR_FILL = 0.35
#: A straight bar is CONVEX: its volume is its hull's. A reducer's step, a venturi's throat or an
#: orifice plate's notch (0.3% of the hull on corpus orifice runs) leaves a hollow in the hull far
#: bigger than the tessellation's.
CONVEX = 0.999
#: How long a body of revolution must be against its width for the flow to run along its axis
#: (a nacelle, a fuselage); a squat one (a dome, a disc) says nothing.
PROLATE = 1.3
#: AN ANNULAR END is a fluid mouth, not a pipe wall's end, when the material round the bore is
#: this thick against the bore's radius: a pipe's wall is a tenth to a fifth of it, an annular
#: blade passage's span two thirds.
THICK_ANNULUS = 0.4
#: ...and the ring spans the part: its outer edge reaches this share of the part across it.
SPANS_PART = 0.8
#: A CONE behind a mouth: the passage widens a little - by no more than CONE_STEP of the mouth's
#: radius per quarter-diameter step, the corpus diffusers' 0.04 to 0.08 - at DIFFUSER_STEPS steps
#: running, past WIDENS and short of OPENS (beyond that it has opened into a chamber). A NARROWING
#: is the same run the other way, by NARROW_STEP at least per step. A branch meeting its header,
#: or a pipe its chamber, jumps instead.
WIDENS, OPENS = 1.02, 2.0
DIFFUSER_STEPS = 3
CONE_STEP, NARROW_STEP = 0.12, 0.02
MAX_PROFILED = 8
#: Openings within this share of the widest one's area are equally wide (a tee's two run ends, the
#: same bore); a housing's 54 and 53 mm ports, 3% apart, are not.
SAME_WIDTH = 0.01
DEPTH_STEPS = tuple(0.25 * k for k in range(1, 25))       # in mouth diameters, to 6 deep
RAYS = 8
#: Triangles tried against the rays at a time: bounds the memory a big skin's cast takes.
RAY_CHUNK = 50_000

#: How sure each reading of the flow direction is.
CONF_ROTOR, CONF_ROTOR_2, CONF_REVOLUTION, CONF_BAR = 0.75, 0.6, 0.7, 0.6
CONF_MIRROR, CONF_EXTRUSION, CONF_CUT = 0.7, 0.6, 0.6
AXES = "xyz"


@dataclass
class Skin:
    tris: np.ndarray            # (n, 3, 3)
    tri_area: np.ndarray
    tri_normal: np.ndarray
    points: np.ndarray          # area-weighted samples
    point_normal: np.ndarray    # the normal of the triangle each sample lies on
    lo: np.ndarray
    hi: np.ndarray
    centroid: np.ndarray        # of the surface, by area
    diag: float
    spacing: float              # mean distance between samples
    tree: Any                   # scipy cKDTree over the samples
    tri_centre: np.ndarray      # each triangle's centre, and how far its corners reach from it
    tri_span: np.ndarray

    @property
    def size(self) -> np.ndarray:
        return self.hi - self.lo


def skin_triangles(path) -> np.ndarray:
    """The skin the check writes (a binary STL) as an (n, 3, 3) array, read in one go; any other
    STL through the general reader."""
    from pathlib import Path

    path = Path(path)
    data = path.read_bytes()
    if len(data) >= 84:
        n = int(np.frombuffer(data, dtype="<u4", count=1, offset=80)[0])
        if len(data) == 84 + 50 * n:
            rec = np.dtype([("normal", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
            return np.frombuffer(data, dtype=rec, count=n, offset=84)["v"].astype(float)
    from meshpipeline.cad.stl_io import read_stl_triangles
    return np.asarray(read_stl_triangles(path), dtype=float).reshape(-1, 3, 3)


def read_skin(tris) -> Skin | None:
    """The skin, sampled, or None when there is too little or too much of it to read."""
    from scipy.spatial import cKDTree

    tris = np.asarray(tris, dtype=float).reshape(-1, 3, 3)
    if len(tris) < 4 or len(tris) > MAX_TRIANGLES:
        return None
    a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
    cross = np.cross(b - a, c - a)
    area = 0.5 * np.linalg.norm(cross, axis=1)
    keep = area > 0
    if not keep.any():
        return None
    tris, cross, area = tris[keep], cross[keep], area[keep]
    total = float(area.sum())
    normal = cross / (2.0 * area[:, None])
    rng = np.random.default_rng(7)                       # the same reading every time
    idx = rng.choice(len(area), size=SAMPLES, p=area / total)
    u, v = rng.random(SAMPLES), rng.random(SAMPLES)
    flip = u + v > 1.0
    u[flip], v[flip] = 1.0 - u[flip], 1.0 - v[flip]
    a = tris[idx, 0]
    pts = a + (tris[idx, 1] - a) * u[:, None] + (tris[idx, 2] - a) * v[:, None]
    corners = tris.reshape(-1, 3)
    lo, hi = corners.min(axis=0), corners.max(axis=0)
    diag = float(np.linalg.norm(hi - lo))
    if not math.isfinite(diag) or diag <= 0:
        return None
    centroid = (area[:, None] * tris.mean(axis=1)).sum(axis=0) / total
    return Skin(tris=tris, tri_area=area, tri_normal=normal, points=pts, point_normal=normal[idx], lo=lo, hi=hi,
                centroid=centroid, diag=diag, spacing=math.sqrt(total / SAMPLES), tree=cKDTree(pts),
                tri_centre=tris.mean(axis=1),
                tri_span=np.linalg.norm(tris - tris.mean(axis=1)[:, None, :], axis=2).max(axis=1))


# ------------------------------------------------------------------------------ symmetry ----
#: A moved sample is BACK ON THE SURFACE when one of its nearest samples lies within a few sample
#: spacings of it along the surface and within this share of the part's diagonal off it - measured
#: square to that sample's own face, so a thin wing mirrored top to bottom lands off its lower skin
#: by its camber, however close the two skins are.
OFF_SURFACE = 0.002
NEIGHBOURS = 6


def _lands(skin: Skin, moved: np.ndarray) -> float:
    """The share of moved samples that land back on the surface."""
    along = 3.0 * skin.spacing
    d, j = skin.tree.query(moved, k=NEIGHBOURS, distance_upper_bound=along)
    found = np.isfinite(d)
    j = np.where(found, j, 0)
    off = moved[:, None, :] - skin.points[j]
    square = np.abs(np.einsum("ijk,ijk->ij", off, skin.point_normal[j]))
    ok = found & (square <= OFF_SURFACE * skin.diag + 1e-12)
    return float(np.mean(ok.any(axis=1)))


def _holds(skin: Skin, move) -> bool:
    """Whether a symmetry holds: `move` maps sample points to where the symmetry puts them. A
    random tenth of the samples screens it first - most tried symmetries fail by far."""
    if _lands(skin, move(skin.points[:SCREEN])) < SCREEN_MATCH:
        return False
    return _lands(skin, move(skin.points)) >= MATCH


def _mirrored(skin: Skin, k: int):
    def move(p: np.ndarray) -> np.ndarray:
        q = p.copy()
        q[:, k] = 2.0 * skin.centroid[k] - q[:, k]
        return q
    return move


def _turn(skin: Skin, k: int, angle: float):
    i, j = (m for m in range(3) if m != k)
    co, si = math.cos(angle), math.sin(angle)

    def move(p: np.ndarray) -> np.ndarray:
        q = p - skin.centroid
        out = q.copy()
        out[:, i] = co * q[:, i] - si * q[:, j]
        out[:, j] = si * q[:, i] + co * q[:, j]
        return out + skin.centroid
    return move


@dataclass
class Symmetry:
    mirror: list[bool]          # mirror-symmetric across the plane square to x, y, z
    order: list[int]            # the highest n-fold turn about x, y, z that holds (0: none)
    revolution: list[bool]      # it lands on itself turned by any angle: a body of revolution
    extruded: list[bool]        # a straight bar along x, y, z

    def as_dict(self) -> dict:
        return {"mirror": self.mirror, "order": self.order, "revolution": self.revolution,
                "extruded": self.extruded}


def symmetry(skin: Skin) -> Symmetry:
    mirror, order, revolution, extruded = [], [], [], []
    for k in range(3):
        mirror.append(_holds(skin, _mirrored(skin, k)))
        rev = all(_holds(skin, _turn(skin, k, 2.0 * math.pi * f)) for f in ODD_TURNS)
        revolution.append(rev)
        best = 0
        if not rev:
            for n in range(MAX_ORDER, 1, -1):
                if _holds(skin, _turn(skin, k, 2.0 * math.pi / n)):
                    best = n
                    break
        order.append(best)
        nk = np.abs(skin.tri_normal[:, k])
        share = float((skin.tri_area * ((nk < 0.04) | (nk > 0.995))).sum() / skin.tri_area.sum())
        extruded.append(share >= EXTRUDED)
    return Symmetry(mirror, order, revolution, extruded)


def _cut_faces(skin: Skin) -> list[float]:
    """For each end of the box (-x, +x, -y, +y, -z, +z): how much of that side of the box a flat
    face lying on it covers."""
    t = skin.tris
    cen = t.mean(axis=1)
    out = []
    for k in range(3):
        side = float(np.prod([skin.size[j] for j in range(3) if j != k])) or 1e-30
        for plane in (skin.lo[k], skin.hi[k]):
            on = (np.abs(skin.tri_normal[:, k]) > 0.995) & (np.abs(cen[:, k] - plane) < 0.004 * skin.diag)
            out.append(float(skin.tri_area[on].sum()) / side)
    return out


def fill_and_hull(skin: Skin) -> tuple[float, float]:
    """How much of its box the part fills, and how much of its convex hull (1.0: convex)."""
    from scipy.spatial import ConvexHull

    a, b, c = (skin.tris[:, k] - skin.centroid for k in range(3))
    vol = abs(float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum())) / 6.0
    box = float(np.prod(np.maximum(skin.size, 1e-30)))
    try:
        hull = float(ConvexHull(np.unique(skin.tris.reshape(-1, 3), axis=0)).volume)
    except Exception:  # noqa: BLE001 - a flat or degenerate skin has no hull
        hull = 0.0
    return vol / box, (vol / hull if hull > 0 else 0.0)


# --------------------------------------------------------------------- the flow direction ----
def direction(skin: Skin, sym: Symmetry) -> list[dict]:
    """Where the flow goes past a body, as the shape says it, surest first: ALONG an axis (a
    rotor's, a body of revolution's) or ACROSS one or more (the flow lies square to a straight
    bar's span, in a mirror plane, along a half model's cut face). The first that settles one axis
    across the up axis is the proposal's (contracts.geometry_fields.axis_from_reading); a mirror
    plane or a flat bottom square to the up axis settles nothing, and the next one speaks. Empty:
    nothing in the shape says, and the longest side is the guess."""
    size = skin.size
    said: list[dict] = []
    rev = [k for k in range(3) if sym.revolution[k]]
    if len(rev) == 1:
        k = rev[0]
        width = max(size[j] for j in range(3) if j != k)
        if sym.extruded[k]:
            said.append(_said("across", k, CONF_BAR, f"a straight round bar along {AXES[k]}: the flow crosses it"))
        elif size[k] >= PROLATE * width:
            said.append(_said("along", k, CONF_REVOLUTION,
                              f"a body of revolution about {AXES[k]}: the flow runs along its axis"))
    hub = rotor_axis(sym)
    if hub is not None:
        top = sym.order[hub]
        said.append(_said("along", hub, CONF_ROTOR if top >= 3 else CONF_ROTOR_2,
                          f"it is {top}-fold symmetric about {AXES[hub]}, like blades round a hub: "
                          "the flow runs along that axis"))
    # a body in a flow is mirrored across its WIDTH (a car, an aircraft); a part mirrored across its
    # own length (a connector, a fitting, a heat sink) says nothing about the flow that way - except
    # a LIFTING SURFACE (thin against its plan): a whole wing is mirrored across its span, which is
    # often its longest side
    thin = int(np.argmin(size))
    lifting = size[thin] <= THIN_BODY * sorted(size)[1]
    longest = int(np.argmax(size))
    mirrors = [k for k in range(3) if sym.mirror[k] and (k != longest or lifting)]
    if mirrors and not rev:
        said.append(_said("across", mirrors, CONF_MIRROR,
                          "it is mirror-symmetric across a plane square to " + " and ".join(AXES[k] for k in mirrors)
                          + ", like a car or an aircraft: the flow runs in that plane"))
    bars = [k for k in range(3) if sym.extruded[k]]
    if len(bars) == 1 and not rev:
        said.append(_said("across", bars[0], CONF_EXTRUSION,
                          f"a straight section along {AXES[bars[0]]}: the flow crosses it"))
    cut = _cut_faces(skin)
    flat_ends = [k for k in range(3) if max(cut[2 * k], cut[2 * k + 1]) >= CUT_FACE]
    # a lifting surface - a wing, a fin, a blade - ends in flat root and tip sections on its span;
    # the flow runs along them, across the span (a plate cut square on every edge says nothing)
    span = [k for k in flat_ends if k != thin]
    if lifting and len(span) == 1 and not sym.mirror[span[0]]:
        k = span[0]
        said.append(_said("across", k, CONF_CUT,
                          f"a thin surface whose ends on {AXES[k]} are flat sections, like a wing's root and tip: "
                          "the flow runs along them, across its span"))
    for end in sorted(range(6), key=lambda e: -cut[e]):
        k = end // 2
        if cut[end] >= CUT_FACE and cut[end ^ 1] < OTHER_END * cut[end] and not sym.mirror[k]:
            said.append(_said("across", k, CONF_CUT,
                              f"one flat face covers the {'-+'[end % 2]}{AXES[k]} end, like a half model's "
                              "symmetry plane or a wing's root: the flow runs along that face"))
    return said


def rotor_axis(sym: Symmetry) -> int | None:
    """The axis blades turn round: the one axis with the highest n-fold turn that holds (n of 3 or
    more, or 2 when no other axis turns at all), on a shape that is no body of revolution and has no
    mirror plane through that axis - blades are pitched or cambered to turn the flow, so a rotor is
    never its own mirror image across a plane holding its axis. A lattice or a box has such mirrors
    and is no rotor. None: no such axis."""
    if any(sym.revolution):
        return None
    top = max(sym.order)
    if top < 2 or sym.order.count(top) != 1:
        return None
    k = sym.order.index(top)
    if top == 2 and max(sym.order[j] for j in range(3) if j != k) > 0:
        return None
    if any(sym.mirror[j] for j in range(3) if j != k):
        return None
    return k


def _said(rule: str, k, conf: float, reason: str) -> dict:
    ks = k if isinstance(k, list) else [k]
    return {"rule": rule, "axes": [AXES[j] for j in ks], "confidence": conf, "reason": reason}


# ------------------------------------------------------------------------- body or passage ----
def body_or_passage(skin: Skin, sym: Symmetry, fill: float, hull: float, facts: dict) -> dict | None:
    """Overrule the scout's internal reading where the whole shape says it is a body in a flow:
    blades round a hub (a passage is never only that), a straight convex bar whose two flat ends
    are all it has, or a file of surfaces with no open end a fluid could pass. None: no change."""
    if facts.get("flow") != "internal":
        return None
    openings = [o for o in facts.get("openings") or [] if o.get("role") != "not_an_opening"]
    k = rotor_axis(sym)
    top = sym.order[k] if k is not None else 0
    if k is not None and top >= 3 and fill < ROTOR_FILL:
        axis = np.zeros(3)
        axis[k] = 1.0
        if all(_on_axis(o, skin, axis) or o.get("kind") == "disc" for o in openings):
            return {"flow": "external", "input_kind": "solid-body", "body_kind": "rotor", "confidence": 0.75,
                    "reason": f"it is {top}-fold symmetric about {AXES[k]} and fills {100 * fill:.0f}% of its box, "
                              "like blades round a hub: it reads as a body in a flow; the openings on its axis are "
                              "its hub's bore"}
    if facts.get("input_kind") == "fluid-domain" and len(openings) == 2 and hull >= CONVEX:
        n0, n1 = (np.asarray(o.get("normal") or [0, 0, 0], dtype=float) for o in openings)
        if float(np.dot(n0, n1)) < -0.95:
            return {"flow": "external", "input_kind": "solid-body", "body_kind": "bar", "confidence": 0.55,
                    "reason": "a straight convex bar whose two flat ends are all it has reads as a body in a flow "
                              "(a cylinder in cross-flow, a stack, a tower, an extruded wing); if it is the fluid "
                              "inside a straight pipe, say so"}
    if facts.get("body_kind") == "surface" and not openings:
        return {"flow": "external", "input_kind": "solid-body", "body_kind": "surface", "confidence": 0.5,
                "reason": "the file holds surfaces with no open end a fluid could pass through, so it reads as a "
                          "body in a flow"}
    return None


def _on_axis(o: dict, skin: Skin, axis: np.ndarray) -> bool:
    c = np.asarray(o.get("centroid_m") or [0, 0, 0], dtype=float)
    n = np.asarray(o.get("normal") or [0, 0, 0], dtype=float)
    off = c - skin.centroid
    radial = np.linalg.norm(off - np.dot(off, axis) * axis)
    return bool(radial <= 0.05 * skin.diag and abs(float(np.dot(n, axis))) >= 0.9)


# ----------------------------------------------------------------------------- the rays ----
def _hits(tris: np.ndarray, origin: np.ndarray, dirs: np.ndarray) -> list[np.ndarray]:
    """For each direction, the sorted distances at which a ray from `origin` crosses the skin
    (Moller-Trumbore, every ray at once, the triangles a chunk at a time)."""
    found: list[list[np.ndarray]] = [[] for _ in range(len(dirs))]
    for k in range(0, len(tris), RAY_CHUNK):
        chunk = tris[k:k + RAY_CHUNK]
        a = chunk[:, 0]
        e1, e2 = chunk[:, 1] - a, chunk[:, 2] - a
        s = origin - a
        q = np.cross(s, e1)                                    # the same for every ray
        p = np.cross(dirs[:, None, :], e2[None, :, :])         # (rays, triangles, 3)
        det = np.einsum("tk,rtk->rt", e1, p)
        ok = np.abs(det) > 1e-18
        inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
        u = np.einsum("tk,rtk->rt", s, p) * inv
        v = (dirs @ q.T) * inv
        t = np.einsum("tk,tk->t", e2, q)[None, :] * inv
        hit = ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 1e-9)
        for r in range(len(dirs)):
            found[r].append(t[r][hit[r]])
    return [np.sort(np.concatenate(f)) if f else np.zeros(0) for f in found]


def _frame(n: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    seed = np.array([1.0, 0.0, 0.0]) if abs(n[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    e1 = seed - np.dot(seed, n) * n
    e1 /= np.linalg.norm(e1)
    return e1, np.cross(n, e1)


def _round_axis(skin: Skin, c: np.ndarray, n: np.ndarray, length: float, radius: float) -> np.ndarray:
    """The triangles that can matter to rays cast across a mouth's axis: those within `radius` of
    the line from `c` inward along -n for `length`. A big skin's other ten thousand are not tried."""
    v = skin.tri_centre - c
    along = -(v @ n)
    across = np.linalg.norm(v + np.outer(along, n), axis=1)
    keep = ((along >= -radius - skin.tri_span) & (along <= length + radius + skin.tri_span)
            & (across <= radius + skin.tri_span))
    return skin.tris[keep]


def profile(skin: Skin, o: dict) -> list[float]:
    """How wide the passage is behind a mouth, step by step inward along its axis, over the
    mouth's own radius - until the straight line in meets a wall (a bend), leaves the part, or the
    passage opens wider than OPENS (a chamber; the last step says so)."""
    c = np.asarray(o["centroid_m"], dtype=float)
    n = np.asarray(o["normal"], dtype=float)
    n = n / (np.linalg.norm(n) or 1.0)
    r0 = 0.5 * float(o.get("diameter_mm") or 0.0) / 1000.0         # the reading's mm of the metres
    if r0 <= 0:
        return []
    e1, e2 = _frame(n)
    angles = np.linspace(0.0, 2.0 * math.pi, RAYS, endpoint=False)
    dirs = np.array([math.cos(t) * e1 + math.sin(t) * e2 for t in angles])
    reach = 1.25 * OPENS * r0
    tris = _round_axis(skin, c, n, 2.0 * r0 * DEPTH_STEPS[-1], reach)
    out: list[float] = []
    start = 0.01 * r0
    ahead = _hits(tris, c - n * start, np.array([-n]))[0]
    wall = float(ahead[0]) + start if len(ahead) else math.inf
    for step in DEPTH_STEPS:
        depth = step * 2.0 * r0
        if depth >= wall:
            break                                     # the straight line in meets a wall
        p = c - n * depth
        first = [h[0] for h in _hits(tris, p, dirs) if len(h) and h[0] <= reach]
        if len(first) < RAYS // 2:
            # nothing within reach most ways round: a chamber, or out of the part - the whole skin says
            whole = [h[0] for h in _hits(skin.tris, p, dirs) if len(h)]
            if len(whole) >= RAYS // 2:
                out.append(float(min(whole + first)) / r0)
            break
        # the NARROWEST way across: a bend's outside wall runs away from the line in, a diffuser's
        # and a chamber's walls run away all round
        out.append(float(min(first)) / r0)
        if out[-1] >= OPENS:
            break                                     # opened into a chamber: that is all it says
    return out


def diffuser_behind(prof: list[float]) -> bool:
    """A CONE behind the mouth: the passage widens a little at every step for a while, as a
    diffuser does - not in one jump, as a branch meets its header or a pipe its chamber."""
    return _run(prof, lambda prev, r: NARROW_STEP <= r - prev <= CONE_STEP and WIDENS <= r < OPENS)


def narrows_behind(prof: list[float]) -> bool:
    """The passage narrows a little at every step for a while: a taper (or a diffuser) seen from
    its wide end, or the straight line in running into a bend's wall."""
    return _run(prof, lambda prev, r: -CONE_STEP <= r - prev <= -NARROW_STEP)


def opens_behind(prof: list[float]) -> bool:
    """The passage opens at once - wider by more than a cone's step in one - as a pipe opens into
    a chamber."""
    return any(r - prev > CONE_STEP for prev, r in zip([1.0] + prof, prof))


def _run(prof: list[float], step) -> bool:
    run = 0
    for prev, r in zip([1.0] + prof, prof):
        run = run + 1 if step(prev, r) else 0
        if run >= DIFFUSER_STEPS:
            return True
    return False


def roles(skin: Skin, facts: dict) -> dict | None:
    """THE INLET BY THE DIFFUSER: a passage that widens gradually behind exactly one of its mouths
    (into a chamber the others open from) is fed through that mouth - a diffuser is passed from its
    narrow end - and leaves by the others. A part that also narrows gradually behind a mouth is a
    taper, which is passed either way (a reducer, a reducing bend): nothing is said then. "inlet"
    None: nothing says (the scout's guess stands, or the widest of a tied pair)."""
    openings = [o for o in facts.get("openings") or [] if o.get("role") != "not_an_opening"]
    if facts.get("flow") != "internal" or not 2 <= len(openings) <= MAX_PROFILED:
        return None
    profs = {o["id"]: profile(skin, o) for o in openings}
    widen = [i for i, p in profs.items() if diffuser_behind(p)]
    taper = any(narrows_behind(p) for p in profs.values())
    # the chamber the cone feeds: another mouth's passage opens into it at once
    chamber = len(widen) == 1 and any(opens_behind(p) for i, p in profs.items() if i != widen[0])
    out = {"inlet": widen[0] if len(widen) == 1 and chamber and not taper else None, "confidence": 0.7,
           "profiles": {str(i): [round(r, 2) for r in p] for i, p in profs.items()},
           "reason": "the passage widens gradually behind it into a chamber, as a diffuser does: the flow comes in there"}
    if out["inlet"] is None:
        tied = widest_tie(openings)
        if tied is not None:
            out.update(inlet=tied, confidence=0.4,
                       reason="it is one of the widest openings, and of those the first along the run they share - "
                              "the end a drawing usually starts from; a guess, so check it")
    return out


def widest_tie(openings: list[dict]) -> int | None:
    """THE FEED AMONG EQUALS: with three or more openings the widest is taken for the feed; when
    two or more are equally wide (a tee's run), the one first along the axis that parts them most -
    the scout's own rule for a pair. None: one opening is the widest on its own (or two openings,
    which the scout orders itself)."""
    if len(openings) < 3:
        return None
    area = {o["id"]: float(o.get("area_mm2") or 0.0) for o in openings}
    top = max(area.values())
    tied = [o for o in openings if area[o["id"]] >= (1.0 - SAME_WIDTH) * top]
    if len(tied) < 2 or top <= 0:
        return None
    pts = np.array([o["centroid_mm"] for o in tied], dtype=float)
    k = int(np.argmax(pts.max(axis=0) - pts.min(axis=0)))
    return int(tied[int(np.argmin(pts[:, k]))]["id"])


def annular_mouths(skin: Skin, facts: dict) -> dict | None:
    """AN ANNULAR FLUID BODY read as a pipe wall: every mouth is a ring whose hole the scout took
    for the bore, but the material round that hole is thick - two thirds of the hole's radius on a
    blade-row passage, where a pipe's wall is a tenth or a fifth. Such a ring is the mouth of the
    fluid itself, an annulus round a hub that is not drawn: the openings are the ring faces (their
    bore the outer edge), and the part is the fluid volume. None: a pipe wall, as read."""
    if facts.get("flow") != "internal" or facts.get("body_kind") != "pipe_wall":
        return None
    openings = [o for o in facts.get("openings") or [] if o.get("role") != "not_an_opening"]
    if len(openings) < 2 or not all(o.get("kind") == "ring" for o in openings):
        return None
    mouths, seed = {}, None

    def across(o: dict) -> float:
        n = np.abs(np.asarray(o["normal"], dtype=float))
        return float(min(skin.size[j] for j in range(3) if j != int(np.argmax(n))))

    # no ring can span the part from a hole this small (the rays reach four of its radii out)
    if any(4.0 * float(o.get("diameter_mm") or 0.0) / 1000.0 < SPANS_PART * across(o) for o in openings):
        return None
    for o in openings:
        found = _wall_round(skin, o)
        if found is None or found[0] < THICK_ANNULUS:
            return None
        # the ring is the part's whole end, as an annular passage's mouth is: a bore drilled into a
        # thick body (a wheel's hub, a block's port) is a hole in it, however thick round it
        outer = float(o.get("diameter_mm") or 0.0) / 1000.0 * (1.0 + found[0])
        if outer < SPANS_PART * across(o):
            return None
        mouths[o["id"]] = found[0]
        seed = seed if seed is not None else found[1]
    return {"thickness": {str(k): round(v, 2) for k, v in mouths.items()}, "seed_m": seed,
            "reason": "every mouth is a ring of material as thick as two fifths of its hole or more: the part reads "
                      "as the fluid in an annulus round a hub that is not drawn, its mouths the ring faces"}


def _wall_round(skin: Skin, o: dict) -> tuple[float, list[float]] | None:
    """How thick the material round a ring's hole is, over the hole's radius (the thinnest of a
    few depths, so a flange at the mouth is not the wall), and a point inside that material."""
    c = np.asarray(o["centroid_m"], dtype=float)
    n = np.asarray(o["normal"], dtype=float)
    n = n / (np.linalg.norm(n) or 1.0)
    r0 = 0.5 * float(o.get("diameter_mm") or 0.0) / 1000.0
    if r0 <= 0:
        return None
    e1, e2 = _frame(n)
    angles = np.linspace(0.0, 2.0 * math.pi, RAYS, endpoint=False)
    dirs = np.array([math.cos(t) * e1 + math.sin(t) * e2 for t in angles])
    tris = _round_axis(skin, c, n, 3.0 * 2.0 * r0, 4.0 * r0)
    # depths behind the mouth that lie inside the part, however short it runs along the axis
    length = float(np.ptp(skin.tris.reshape(-1, 3) @ n))
    depths = [f * 2.0 * r0 for f in (0.25, 0.75, 1.5) if f * 2.0 * r0 < 0.9 * length] or [0.5 * length]
    best, inside = None, None
    for depth in depths:
        p = c - n * depth
        widest, at = None, None
        for d, h in zip(dirs, _hits(tris, p, dirs)):
            k = np.searchsorted(h, 0.9 * r0)
            if k + 1 >= len(h) or h[k] > 1.1 * r0:
                continue                              # no bore wall at the hole's radius this way
            t = (h[k + 1] - h[k]) / r0
            if widest is None or t > widest:
                widest, at = t, p + d * 0.5 * (h[k] + h[k + 1])
        if widest is None:
            continue
        if best is None or widest < best:
            best, inside = widest, at
    if best is None or inside is None:
        return None
    return best, [round(float(v), 6) for v in inside]


# ---------------------------------------------------------------------------- everything ----
def read_flow(tris, facts: dict) -> dict:
    """Everything the skin says, as plain data (never raises for a shape it cannot read)."""
    skin = read_skin(tris)
    if skin is None:
        return {"read": False, "reason": "the skin is too small or too large to read quickly"}
    sym = symmetry(skin)
    fill, hull = fill_and_hull(skin)
    out = {"read": True, "symmetry": sym.as_dict(), "fill": round(fill, 3), "hull": round(hull, 3),
           "direction": direction(skin, sym)}
    kind = body_or_passage(skin, sym, fill, hull, facts)
    if kind:
        out["kind"] = kind
    else:
        ann = annular_mouths(skin, facts)
        if ann:
            out["annulus"] = ann
        r = roles(skin, facts)
        if r:
            out["roles"] = r
    return out


def apply_reading(facts: dict, reading: dict) -> None:
    """Fold the reading into the scout's facts: the kind and flow it overrules, the inlet it
    names, and the direction the proposal reads (contracts.geometry_fields.external_defaults)."""
    facts["reading"] = reading
    if not reading.get("read"):
        return
    kind = reading.get("kind")
    if kind:
        facts["flow"], facts["input_kind"] = kind["flow"], kind["input_kind"]
        facts["body_kind"] = kind.get("body_kind") or facts.get("body_kind")
        facts.setdefault("confidence", {})["input_kind"] = kind["confidence"]
        if kind["flow"] == "external":
            # A BODY IN A FLOW KEEPS ITS LIDS, as both scouts keep them for a body they read
            # themselves: the openings it has IF the fluid flows through it (a stirred tank's
            # rotating zone, a shaft: a closed cylinder whose two flat ends are its mouths). They
            # are not drawn (geometry_check.stickered) and the confirm route stores none for an
            # external body, so they cost nothing until the user says the fluid flows through it -
            # and then there is something to confirm instead of nothing. No point inside the flow
            # is sought for a body.
            facts["seed_point_mm"] = facts["seed_point_m"] = None
        facts.setdefault("notes", []).append(kind["reason"])
    ann = reading.get("annulus")
    if ann:
        facts["body_kind"], facts["input_kind"] = "annular_fluid", "fluid-domain"
        facts.setdefault("confidence", {})["input_kind"] = 0.6
        for o in facts.get("openings") or []:
            t = (ann["thickness"] or {}).get(str(o.get("id")))
            if t is None:
                continue
            inner = float(o.get("diameter_mm") or 0.0)
            outer = inner * (1.0 + float(t))
            o["kind"], o["shape"] = "annulus", "circle"
            o["inner_diameter_mm"], o["diameter_mm"] = round(inner, 2), round(outer, 2)
            o["area_mm2"] = round(math.pi / 4.0 * (outer ** 2 - inner ** 2), 2)
            o["area_m2"] = round(o["area_mm2"] * 1e-6, 9)
            o.pop("width_mm", None)
            o.pop("height_mm", None)
        seed = ann.get("seed_m")
        if seed:
            facts["seed_point_m"] = seed
            facts["seed_point_mm"] = [round(v * 1000.0, 2) for v in seed]
        facts.setdefault("notes", []).append(ann["reason"])
    r = reading.get("roles")
    ops = [o for o in facts.get("openings") or [] if o.get("role") != "not_an_opening"]
    if r and r.get("inlet") is not None and any(o["id"] == r["inlet"] and o.get("role") != "inlet" for o in ops):
        outs = [o for o in ops if o["id"] != r["inlet"]]
        for o in ops:
            if o["id"] == r["inlet"]:
                o["name"], o["role"] = "inlet", "inlet"
        for k, o in enumerate(outs, start=1):
            o["name"], o["role"] = (f"outlet_{k}" if len(outs) > 1 else "outlet"), "outlet"
        facts.setdefault("notes", []).append(f"opening {r['inlet']} is the inlet: {r['reason']}")
    d = reading.get("direction")
    if d:
        facts["flow_axis_reading"] = d


__all__ = ["Skin", "Symmetry", "apply_reading", "body_or_passage", "diffuser_behind", "direction",
           "fill_and_hull", "narrows_behind", "opens_behind", "profile", "read_flow", "read_skin", "roles", "skin_triangles",
           "symmetry", "widest_tie"]
