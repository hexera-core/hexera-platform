# Responsibility: Find a part's WALL PAIRS - two CAD faces of one body that face each other across a wall of
# even thickness - and say, by measured thickness against the wall's own in-plane size, which walls are thin
# enough to be a shell, and what every other face is (a plate's rim, a small blend, a thick wall, solid).
# Boundaries: geometry only, deterministic, no model call, no persistence. The pairs come from the same rays
# that measure the FEA walls (cad/fea_features.wall_reach: a ray from a face through the material reads the
# face it leaves by); each is then checked on the exact B-rep (cad/midsurface builds the mid-faces).
# Collaborates with: cad/fea_measure.py (the labelled surface and Face_k order), cad/fea_features.py (the
# rays), cad/midsurface.py (the mid-faces and their widths, the body verdicts), docs/design/shell/README.md.
"""WALL PAIRS (SHELL-1) AND THE ROLE OF EVERY FACE (SHELL-2).

A pair is two faces A, B of one body such that rays fired inward from A leave the material through B, the
exact distance from A to B is the same everywhere it is sampled (within EVEN_SHARE), and the faces' normals
there are opposite (within PAIR_DEG). Such a wall has a mid-face at half its thickness from both faces, the
half-offset of either face (cad/midsurface). The surface type does not matter: two planes, two coaxial
cylinders or cones, two concentric spheres, two offset free-form faces are all the same rule.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from meshpipeline.cad import fea_features as ff

#: How many rays read the pairs, spread over the part's faces by area, and at least this many on every face,
#: so a narrow face is read too. A fixed seed: a part reads the same twice.
PAIR_RAYS = 20_000
MIN_FACE_RAYS = 24
#: A face B is A's partner when at least PAIR_MIN_RAYS of A's rays leave through B, and at least PAIR_SHARE of
#: A's rays that read a wall do (a face split in two by a stiffener has two partners, each about half).
PAIR_MIN_RAYS = 3
PAIR_SHARE = 0.1
#: THE EVEN-WALL TEST, on the exact faces: points spread over A (PAIR_SAMPLES x PAIR_SAMPLES over its
#: parameters, the ones inside the face) are projected onto B's surface. The pair is an even wall when the
#: distances agree within EVEN_SHARE of their median, and B's normal at each foot is opposite A's within
#: PAIR_DEG. A taper, a wedge, a wall that thickens are VARIABLE walls: named, mid-faced later (design note).
PAIR_SAMPLES = 7
EVEN_SHARE = 0.02
PAIR_DEG = 2.0
#: A TAPERED WALL (SHELL-9, variable thickness): a pair that is not even, whose faces turn from each other by at
#: most TAPER_DEG where they face each other (the points of A whose foot lies on B itself, at least
#: TAPER_MIN_POINTS of them). Its mid-surface is fitted through the midpoints (cad/midsurface.fitted_mid) and its
#: thickness varies over it; whether it is a shell is judged at its THICKEST point (SHELL-2). A draft, a wedge, a
#: printed funnel's drafted wall. Steeper than that, the two faces are two sides of a solid region, not one wall.
TAPER_DEG = 10.0
TAPER_MIN_POINTS = 6
#: SHELL OR SOLID (SHELL-2) is judged per SHEET: the even wall pairs of one thickness (within EVEN_SHARE) whose
#: faces are the same or meet at a CAD edge - a bent sheet's flanges and bends, a deck plate's pieces between
#: stiffeners. A sheet is a shell when its thickness is at most SHELL_RATIO of its size L, the square root of
#: its mid-surface area (shell theory - Mindlin plates, the S4R / CQUAD4 kind - holds to t / L of about 1/10,
#: L a typical structural size), AND at most SHELL_WIDTH_RATIO of its width (cad/midsurface.width_of), so a
#: shell element there can be several walls wide.
SHELL_RATIO = 0.1
SHELL_WIDTH_RATIO = 0.25
#: A face that pairs with nothing is a plate's RIM (its edge face, a hole's bore through the plate) when its
#: own width is at most RIM_FACTOR times the thickness of a shell wall it touches, and a BLEND (a fillet or a
#: chamfer where walls meet) when it is round with radius at most BLEND_FACTOR times that thickness, or flat
#: and no wider than BLEND_FACTOR times it. Both are taken up by the mid-surface: a rim is its free edge, a
#: blend is bridged when the mid-faces are extended to meet.
RIM_FACTOR = 1.5
BLEND_FACTOR = 2.0
#: ... and a round blend is a STRIP: its own width (2 x area / perimeter, the arc it turns through) is at most
#: half a turn of that largest radius. A 250 mm long 16 mm rod hung under a 5 mm plate is round with radius 8,
#: but it is no fillet: it is a solid region
BLEND_ARC = math.pi * BLEND_FACTOR

#: the roles a face can have
PAIRED, THICK, RIM, BLEND, SOLID = "paired", "thick", "rim", "blend", "solid"


@dataclass
class Pair:
    """Two faces (0-based, MapShapes order) across an even wall."""
    a: int
    b: int
    thickness: float                      # mm, the exact median distance
    spread: float                         # (max - min) / median of the exact distances
    rays: int                             # rays that crossed from one to the other (both ways)
    even: bool                            # the even-wall test passed
    why: str = ""                         # what failed, when it did
    width: float | None = None            # mm, its SHEET's in-plane width (cad/midsurface)
    size: float | None = None             # mm, its sheet's size: the square root of the sheet's mid area
    sheet: int | None = None              # which sheet (0-based) it belongs to
    shell: bool = False                   # its sheet is thin against its size and width (SHELL-2)
    mids: list[int] = field(default_factory=list)   # the mid-faces built from it (cad/midsurface)
    tapered: bool = False                 # a TAPERED wall (SHELL-9): not even, but turned at most TAPER_DEG
    t_min: float | None = None            # mm, a tapered wall's thinnest and thickest reading
    t_max: float | None = None

    def span(self) -> tuple[float, float]:
        """The wall's thinnest and thickest reading, mm (an even wall's are its thickness)."""
        if self.tapered and self.t_min is not None and self.t_max is not None:
            return self.t_min, self.t_max
        return self.thickness, self.thickness


def _sample_rays(s: ff.Surface, *, rays: int = PAIR_RAYS, seed: int = 0):
    """Points spread over every face by area (at least MIN_FACE_RAYS each), each with its inward normal."""
    rng = np.random.default_rng(seed)
    total = float(s.areas.sum()) or 1.0
    origins, dirs, labels = [], [], []
    for lab in np.unique(s.label):
        idx = np.flatnonzero((s.label == lab) & (s.areas > 0))
        if not len(idx):
            continue
        area = float(s.areas[idx].sum())
        n = max(MIN_FACE_RAYS, int(round(rays * area / total)))
        pick = idx[rng.choice(len(idx), size=n, p=s.areas[idx] / area)]
        r1, r2 = rng.random(n), rng.random(n)
        root = np.sqrt(r1)
        a, b, c = (s.xyz[s.tris[pick, k]] for k in range(3))
        origins.append((1 - root)[:, None] * a + (root * (1 - r2))[:, None] * b + (root * r2)[:, None] * c)
        dirs.append(-s.normals[pick])
        labels.append(np.full(n, lab))
    return (np.concatenate(origins), np.concatenate(dirs), np.concatenate(labels)) if origins else (None, None, None)


def ray_partners(s: ff.Surface) -> dict[tuple[int, int], int]:
    """{(a, b) with a < b: rays that crossed between them, both ways} for every pair that passes the ray
    test (PAIR_MIN_RAYS and PAIR_SHARE, read from either side)."""
    P, D, L = _sample_rays(s)
    if P is None:
        return {}
    eps = 1e-6 * s.diag
    hit = np.full(len(P), -1, dtype=np.int64)
    ff.wall_reach(s, P + D * eps, D, L, eps=eps, exit_label=hit)
    ok = hit >= 0
    m = len(s.names)
    keys, counts = np.unique(L[ok] * m + hit[ok], return_counts=True)
    read = np.bincount(L[ok], minlength=m)
    both: dict[tuple[int, int], int] = {}
    nominated: set[tuple[int, int]] = set()
    for k, c in zip(keys.tolist(), counts.tolist(), strict=True):
        a, b = divmod(int(k), m)
        if a == b:
            continue
        key = (min(a, b), max(a, b))
        both[key] = both.get(key, 0) + int(c)
        if c >= PAIR_MIN_RAYS and c >= PAIR_SHARE * read[a]:
            nominated.add(key)
    return {k: both[k] for k in sorted(nominated)}


#: A surface that runs to infinity one way (an extrusion, a revolution's profile, an offset of either - not the
#: analytic plane, cylinder, cone) is projected on within the partner face's own parameter box grown by
#: PROJECT_SPAN times its span on every side: the foot of a wall point lies by its partner face, and a search over
#: an unbounded parameter ran for 18 s a pair (a fan housing's extruded skins, 400 pairs).
PROJECT_SPAN = 1.0


#: the projectors kept, one per face (a face is the partner of many pairs: a fan housing's skin of 70)
_PROJECTORS: dict[int, tuple] = {}
PROJECTORS_KEPT = 2048


def _projector(fb):
    """One point projector onto face fb's surface, built once for every point read on it."""
    key = hash(fb)
    got = _PROJECTORS.get(key)
    if got is not None and got[0].IsSame(fb):
        return got[1]
    if len(_PROJECTORS) >= PROJECTORS_KEPT:
        _PROJECTORS.clear()
    proj = _new_projector(fb)
    _PROJECTORS[key] = (fb, proj)
    return proj


def _new_projector(fb):
    from OCP.BRep import BRep_Tool
    from OCP.BRepTools import BRepTools
    from OCP.GeomAbs import GeomAbs_Cone, GeomAbs_Cylinder, GeomAbs_Plane, GeomAbs_Sphere, GeomAbs_Torus
    from OCP.GeomAdaptor import GeomAdaptor_Surface
    from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
    from OCP.Precision import Precision

    surf = BRep_Tool.Surface_s(fb)
    proj = GeomAPI_ProjectPointOnSurf()
    s0, s1, t0, t1 = surf.Bounds()
    infinite = any(Precision.IsInfinite_s(x) for x in (s0, s1, t0, t1))
    analytic = GeomAdaptor_Surface(surf).GetType() in (GeomAbs_Plane, GeomAbs_Cylinder, GeomAbs_Cone, GeomAbs_Sphere,
                                                       GeomAbs_Torus)
    if infinite and not analytic:
        u0, u1, v0, v1 = BRepTools.UVBounds_s(fb)
        du, dv = PROJECT_SPAN * max(u1 - u0, 1e-9), PROJECT_SPAN * max(v1 - v0, 1e-9)
        s0, s1 = max(s0, u0 - du), min(s1, u1 + du)
        t0, t1 = max(t0, v0 - dv), min(t1, v1 + dv)
    proj.Init(surf, s0, s1, t0, t1)
    return proj


def even_wall(fa, fb, mm: float) -> tuple[bool, float, float, str]:
    """The exact even-wall test (PAIR_SAMPLES, EVEN_SHARE, PAIR_DEG) of OpenCASCADE faces fa, fb, whose
    coordinates are `mm` millimetres each: (passed, median thickness mm, spread, why not)."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepClass import BRepClass_FaceClassifier
    from OCP.BRepLProp import BRepLProp_SLProps
    from OCP.gp import gp_Pnt2d
    from OCP.TopAbs import TopAbs_IN, TopAbs_REVERSED

    sa = BRepAdaptor_Surface(fa, True)
    proj = _projector(fb)
    u0, u1, v0, v1 = sa.FirstUParameter(), sa.LastUParameter(), sa.FirstVParameter(), sa.LastVParameter()
    if not all(math.isfinite(x) for x in (u0, u1, v0, v1)):
        return False, float("nan"), float("nan"), "a face without bounds"
    sign_a = -1.0 if fa.Orientation() == TopAbs_REVERSED else 1.0
    sign_b = -1.0 if fb.Orientation() == TopAbs_REVERSED else 1.0
    tol = max(BRep_Tool.Tolerance_s(fa), 1e-9)
    dists, turned = [], []
    n = PAIR_SAMPLES
    for i in range(n):
        for j in range(n):
            u = u0 + (u1 - u0) * (i + 0.5) / n
            v = v0 + (v1 - v0) * (j + 0.5) / n
            if BRepClass_FaceClassifier(fa, gp_Pnt2d(u, v), tol).State() != TopAbs_IN:
                continue
            pa = BRepLProp_SLProps(sa, u, v, 1, 1e-9)
            if not pa.IsNormalDefined():
                continue
            p = pa.Value()
            proj.Perform(p)
            if proj.NbPoints() < 1:
                continue
            ub, vb = proj.LowerDistanceParameters()
            pb = BRepLProp_SLProps(BRepAdaptor_Surface(fb, True), ub, vb, 1, 1e-9)
            if not pb.IsNormalDefined():
                continue
            na, nb = pa.Normal(), pb.Normal()
            dot = sign_a * sign_b * (na.X() * nb.X() + na.Y() * nb.Y() + na.Z() * nb.Z())
            dists.append(float(proj.LowerDistance()) * mm)
            turned.append(math.degrees(math.acos(max(-1.0, min(1.0, -dot)))))
    if len(dists) < 3:
        return False, float("nan"), float("nan"), "too few points of the face could be read"
    d = np.asarray(dists)
    med = float(np.median(d))
    spread = float((d.max() - d.min()) / med) if med > 0 else float("inf")
    if med <= 0:
        return False, med, spread, "the faces touch"
    if max(turned) > PAIR_DEG:
        return False, med, spread, f"the faces are not parallel (they turn by up to {max(turned):.1f} degrees)"
    if spread > EVEN_SHARE:
        return False, med, spread, f"the wall is not even (it varies by {100 * spread:.0f}%)"
    return True, med, spread, ""


def tapered_wall(fa, fb, mm: float) -> tuple[bool, float, float, float, str]:
    """The TAPERED-wall test (SHELL-9) of faces fa, fb whose coordinates are `mm` millimetres each: points spread
    over fa (PAIR_SAMPLES x PAIR_SAMPLES over its parameters, the ones inside it) are projected onto fb's surface;
    only those whose foot lies on fb itself are read. (passed, thinnest mm, thickest mm, largest turn degrees,
    why not)."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepClass import BRepClass_FaceClassifier
    from OCP.BRepLProp import BRepLProp_SLProps
    from OCP.gp import gp_Pnt2d
    from OCP.TopAbs import TopAbs_IN, TopAbs_ON, TopAbs_REVERSED

    nan = float("nan")
    sa = BRepAdaptor_Surface(fa, True)
    sb = BRepAdaptor_Surface(fb, True)
    proj = _projector(fb)
    u0, u1, v0, v1 = sa.FirstUParameter(), sa.LastUParameter(), sa.FirstVParameter(), sa.LastVParameter()
    if not all(math.isfinite(x) for x in (u0, u1, v0, v1)):
        return False, nan, nan, nan, "a face without bounds"
    sign = (-1.0 if fa.Orientation() == TopAbs_REVERSED else 1.0) * (-1.0 if fb.Orientation() == TopAbs_REVERSED
                                                                     else 1.0)
    tol_a = max(BRep_Tool.Tolerance_s(fa), 1e-9)
    tol_b = max(BRep_Tool.Tolerance_s(fb), 1e-9)
    dists, turned = [], []
    n = PAIR_SAMPLES
    for i in range(n):
        for j in range(n):
            u = u0 + (u1 - u0) * (i + 0.5) / n
            v = v0 + (v1 - v0) * (j + 0.5) / n
            if BRepClass_FaceClassifier(fa, gp_Pnt2d(u, v), tol_a).State() != TopAbs_IN:
                continue
            pa = BRepLProp_SLProps(sa, u, v, 1, 1e-9)
            if not pa.IsNormalDefined():
                continue
            proj.Perform(pa.Value())
            if proj.NbPoints() < 1:
                continue
            ub, vb = proj.LowerDistanceParameters()
            if BRepClass_FaceClassifier(fb, gp_Pnt2d(ub, vb), tol_b).State() not in (TopAbs_IN, TopAbs_ON):
                continue
            pb = BRepLProp_SLProps(sb, ub, vb, 1, 1e-9)
            if not pb.IsNormalDefined():
                continue
            na, nb = pa.Normal(), pb.Normal()
            dot = sign * (na.X() * nb.X() + na.Y() * nb.Y() + na.Z() * nb.Z())
            dists.append(float(proj.LowerDistance()) * mm)
            turned.append(math.degrees(math.acos(max(-1.0, min(1.0, -dot)))))
            if turned[-1] > TAPER_DEG:
                # one point turned past the limit settles it: no taper (most pairs the rays find across a
                # corner are two sides of a solid; reading all 49 points of each was half a fan housing's pairing)
                return False, nan, nan, turned[-1], f"the faces are not parallel (they turn by {turned[-1]:.1f} degrees)"
    if len(dists) < TAPER_MIN_POINTS:
        return False, nan, nan, nan, "too few points of the faces face each other"
    lo, hi, turn = float(min(dists)), float(max(dists)), float(max(turned))
    if lo <= 0:
        return False, lo, hi, turn, "the faces touch"
    if turn > TAPER_DEG:
        return False, lo, hi, turn, f"the faces are not parallel (they turn by up to {turn:.1f} degrees)"
    return True, lo, hi, turn, ""


def _same_wall(p: Pair, q: Pair) -> bool:
    """Two pairs are of one thickness (SHELL-2): within EVEN_SHARE, or - a tapered wall - their spans overlap
    within it."""
    if not p.tapered and not q.tapered:
        return abs(p.thickness - q.thickness) <= EVEN_SHARE * max(p.thickness, q.thickness)
    (a0, a1), (b0, b1) = p.span(), q.span()
    return a0 <= b1 * (1 + EVEN_SHARE) and b0 <= a1 * (1 + EVEN_SHARE)


def sheets(pairs: list[Pair], neighbours: list[set[int]], body_of: list[int], *,
           need_mids: bool = True) -> list[list[int]]:
    """The SHEETS (SHELL-2): groups of even or tapered pairs (indices into `pairs`, those with mid-faces) of one
    body and one thickness (within EVEN_SHARE; a tapered wall's span) whose faces are shared or meet at a CAD
    edge. With need_mids False, every even or tapered pair (before mid-faces are made: SHELL-2b's walls).
    Deterministic: in pair order."""
    live = [i for i, p in enumerate(pairs) if (p.even or p.tapered) and (p.mids or not need_mids)]
    parent = {i: i for i in live}

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for x, i in enumerate(live):
        pi = pairs[i]
        near_i = {pi.a, pi.b} | neighbours[pi.a] | neighbours[pi.b]
        for j in live[x + 1:]:
            pj = pairs[j]
            if body_of[pi.a] != body_of[pj.a]:
                continue
            if not _same_wall(pi, pj):
                continue
            if {pj.a, pj.b} & near_i:
                ri, rj = root(i), root(j)
                if ri != rj:
                    parent[max(ri, rj)] = min(ri, rj)
    groups: dict[int, list[int]] = {}
    for i in live:
        groups.setdefault(root(i), []).append(i)
    return [groups[k] for k in sorted(groups)]


def laps(pairs: list[Pair]) -> list[int]:
    """The LAPS (SHELL-8): even pairs, not shell by their own sheet, that are two shell plates stacked in one body.
    A pair (A, B) is a lap when face A is also a side of a shell pair (A, C), face B a side of another shell pair
    (B, D), and its wall is the two walls added up (within EVEN_SHARE): two plates laid one on the other (a lap
    joint, a doubler, a bracket flange on a plate) and fused. A lap is only a few walls wide, so it fails the
    width rule on its own; it is as much a shell as the two plates it is made of, and is shelled with them (its
    mid-face at its own wall; SHELL-7 then snaps the two plates onto it with their offsets). Deterministic: in
    pair order."""
    shell_of: dict[int, list[int]] = {}
    for i, p in enumerate(pairs):
        if p.even and p.shell and p.mids:
            shell_of.setdefault(p.a, []).append(i)
            shell_of.setdefault(p.b, []).append(i)
    out = []
    for i, p in enumerate(pairs):
        if not p.even or p.shell or not p.mids:
            continue
        if any(q != r and abs(p.thickness - (pairs[q].thickness + pairs[r].thickness)) <= EVEN_SHARE * p.thickness
               for q in shell_of.get(p.a, []) for r in shell_of.get(p.b, [])):
            out.append(i)
    return out


def face_roles(names: list[str], pairs: list[Pair], *, kinds: list[str], widths: list[float],
               radii: list[float | None], neighbours: list[set[int]],
               arcs: list[float] | None = None) -> list[dict]:
    """Each face's role (SHELL-2): PAIRED (a side of a shell wall), THICK (a side only of walls too thick to
    be a shell), RIM, BLEND or SOLID - with the pairs it is in and, for a rim or a blend, the shell wall that
    takes it up. `widths`: each face's own in-plane width (mm); `radii`: its radius when it is round; `arcs`:
    its developed width (2 x area / perimeter, mm, not capped by its box: a rod reads its length)."""
    arcs = arcs if arcs is not None else widths
    shell_t: dict[int, float] = {}
    in_pairs: dict[int, list[int]] = {}
    for i, p in enumerate(pairs):
        for f in (p.a, p.b):
            in_pairs.setdefault(f, []).append(i)
            if p.shell:
                shell_t[f] = max(shell_t.get(f, 0.0), p.thickness)
    out = []
    for k, name in enumerate(names):
        row: dict = {"face": name, "kind": kinds[k], "pairs": [f"P{i + 1}" for i in in_pairs.get(k, [])]}
        if k in shell_t:
            row["role"] = PAIRED
        else:
            near = [shell_t[j] for j in neighbours[k] if j in shell_t]
            t = max(near) if near else None
            r = radii[k]
            if t is not None and kinds[k] == "plane" and widths[k] <= RIM_FACTOR * t:
                row["role"] = RIM
            elif (t is not None and r is not None and r <= BLEND_FACTOR * t and kinds[k] != "plane"
                  and arcs[k] <= BLEND_ARC * t):
                row["role"] = BLEND
            elif t is not None and widths[k] <= RIM_FACTOR * t:
                row["role"] = RIM          # a round rim: a hole's bore through a plate, a rounded plate edge
            elif (t is not None and kinds[k] == "plane" and widths[k] <= BLEND_FACTOR * t
                  and not any(pairs[i].even for i in in_pairs.get(k, []))):
                # a chamfer - which faces no parallel face across the metal: a narrow flat face that does (the end
                # of a plug inside a tube, 11 mm from its other end) is a side of a thick region
                row["role"] = BLEND
            elif k in in_pairs:
                row["role"] = THICK
            else:
                row["role"] = SOLID
            if t is not None and row["role"] in (RIM, BLEND):
                row["wall_mm"] = float(f"{t:.6g}")
        out.append(row)
    return out


__all__ = ["BLEND", "PAIRED", "RIM", "SHELL_RATIO", "SHELL_WIDTH_RATIO", "SOLID", "TAPER_DEG", "THICK", "Pair",
           "even_wall", "face_roles", "sheets", "ray_partners", "tapered_wall"]
