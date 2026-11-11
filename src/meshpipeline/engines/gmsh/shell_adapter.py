# Responsibility: Mesh a part's sewn mid-surface (cad/midsurface: midsurface.brep + shell.json) with gmsh as a
# quad-dominant shell mesh - each face sized by the shell plan (engines/shell_plan.py), conformal along every
# junction because the faces share their edges - and hand back the engine-neutral ShellMesh: its elements'
# walls, mid-faces, parts and groups, the normals of each sheet turned one way, and its measured quality.
# Boundaries: runs gmsh in this process (the caller decides which process); reads the two files it is given
# and writes nothing. Deterministic: one thread, fixed options.
# Collaborates with: cad/midsurface.py, engines/shell_plan.py, engines/shell_mesh.py, engines/shell_quality.py,
# engines/shell_writers.py.
from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path

import numpy as np

from meshpipeline.engines import shell_mesh, shell_quality
from meshpipeline.engines import shell_plan as plan_rules
from meshpipeline.engines import shell_requirements as SR
from meshpipeline.engines.shell_mesh import ShellMesh, edge_census, orient

logger = logging.getLogger(__name__)

#: gmsh: Frontal-Delaunay, then Blossom recombination into quads; quad-dominant (a few triangles where quads cannot
#: fit). Frontal-Delaunay-for-quads (8) left faces of B-spline mid-surfaces 7 times coarser than their size and
#: their quads past Nastran limits; QuadQS (11) made 3 to 5 times the elements asked for
ALGORITHM = 6
RECOMBINATION = 1
#: SHMESH-10: cut every quad past Nastran's own limits into two triangles
SPLIT_PAST = True
#: SHMESH-11: cut every closed sewn face at its seams before meshing; a face gmsh still cannot mesh as a periodic
#: surface is re-made as a B-spline (the same surface, exactly) and the mesh made again - at most RESPLINE_MAX faces
SPLIT_CLOSED = True
#: SHMESH-14: before any quad is cut, the inner nodes of flat faces round a quad past a limit are moved towards their
#: neighbours' middle when that brings fewer elements past a limit (at most RELAX_ROUNDS passes)
RELAX_ROUNDS = 6
#: SHMESH-14c: the nodes inside curved faces relax too, each tried point laid back onto the face's surface
RELAX_CURVED = True
#: SHMESH-13: swap the longest edge of a triangle past a limit with its neighbour's when both come out better
FLIP_PAST = True
#: SHMESH-12: merge the ends of sliver edges (under the floor and a fifth of their elements' longest edges)
COLLAPSE_SHORT = True
RESPLINE_MAX = 16
#: SHMESH-11b: an analytic face (a cylinder, a cone) whose boundary gmsh's 2D mesher cannot recover ("impossible to
#: recover the edge") is re-made as a B-spline (the same surface, exactly, without its period) and the mesh made
#: again. Seen on a ship's 3 m hull cylinder with 25 mm slots (sh0048): the same face scaled down by 10 meshed, and
#: so did its B-spline at full size - gmsh's handling of the large periodic analytic surface, not the face's shape
RESPLINE_UNRECOVERED = True
#: SHMESH-11c: a face whose 1D mesh gmsh reads as not a closed loop is re-made as a B-spline too
RESPLINE_LOOP = True
#: ... at most this many faces in one mesh (a duct whose faces failed one after another, 14 re-made, then hung gmsh)
RESPLINE_LOOP_MAX = 4
#: SHMESH-11e: no face is re-made once the meshes so far have taken this long (each re-made face meshes it all again)
RESPLINE_BUDGET_S = 300.0
#: SHMESH-11g: faces on offset surfaces are meshed as B-splines
OFFSETS_AS_SPLINES = True
#: SHMESH-20: the most elements a shell plan may ask for (area / size^2 summed over its faces; gmsh makes about 1,000
#: a second here, and its counts run 0.4 to 3.3 times the plan's on the corpus)
PLANNED_MAX = 1.5e6
#: SHMESH-21: a shell mesh is stopped after this long (0: unbounded)
MESH_DEADLINE_S = 900.0
#: SHMESH-17: two triangles of a face that together make a quad inside Nastran's own limits are joined into it
PAIR_TRIAS = True
#: SHMESH-18: a triangle walked through at most WALK_DEPTH quads of its face to meet another (0: off)
WALK_DEPTH = 6
#: SHMESH-22: a cap triangle past a limit taken into the element across its longest edge, or dropped from a free edge
ABSORB_CAPS = True
#: SHMESH-19: every face's boundary given an even number of segments before the 2D mesh (Blossom's condition)
EVEN_FACES = True
#: at most GROW_GROUPS grading sizes; past that, sizes taken down to steps of GROW_STEP (a quarter octave)
GROW_GROUPS = 48
GROW_STEP = 2.0 ** 0.25
#: how closely gmsh integrates the size field along a curve to place its nodes: gmsh's own 1e-9 kept a ship's 1D mesh
#: (24,000 curves, sh0048) going for over 30 minutes; 1e-3 places nodes to a thousandth (counts within 0.2%)
LC_PRECISION = 1e-3
#: SHMESH-16: narrow four-sided strips meshed as mapped grids, stretched along them up to STRETCH_MAX times their
#: size across (Femap's aspect target: 5); a strip's opposite sides alike within STRIP_ALIKE, its long sides at
#: least STRIP_LONG times its ends
MAP_STRIPS = True
STRETCH_MAX = 5.0
MAP_FLAT = False
STRETCH_FLAT = 1.0
STRIP_ALIKE = 0.05
STRIP_LONG = 1.5
EVEN = True
#: SHMESH-3/4 through gmsh's own curvature sizing: elements per full turn of every curve and curved face
PER_TURN = 4 * plan_rules.CURVE_PER_QUARTER
#: SHMESH-27 (under the user's own sizes only): the size beside a CAD edge shorter than the size, and on a face
#: narrower than it, is held at shell_plan.FEATURE_RATIO times the feature and grown from there
FEATURE_SIZES = True
FEATURE_EDGES = True
#: (faces: measured on the 80 lab parts, free-meshed narrow faces gave 4,300 more triangles and 3 parts past Nastran's
#: limits for the same parts inside the list; off until narrow faces of more than four sides are mapped)
FEATURE_FACES = False
#: SHMESH-30: a sheet drawn as many CAD patches is sized by the sheet (shell_plan.SHEET_SMOOTH_DEG) - on surface
#: files (record "source": "surface", PR #434's route, not on main); on extracted mid-surfaces too only with
#: SHEET_REGIONS_ALL (a change to the rules' own mesh: off until signed off)
SHEET_REGIONS = True
SHEET_REGIONS_ALL = False
#: SHMESH-31: how many times faces holding elements past the solver's own limits under the user's sizes are meshed
#: again at our own (finer) sizes there
OWN_SIZE_RETRIES = 2
SPOTS_MAX = 64
#: SHMESH-35: the walls of beads, darts and steps keep shell_plan.FLANK_ACROSS elements across under a coarser size
FLANKS = True
#: SHMESH-34: under the user's sizes, how far element centres stand off curved faces is measured and told (at most
#: CHORD_SAMPLES elements a face are read)
CHORD_REPORT = True
CHORD_SAMPLES = 200
#: SHMESH-32: gmsh's own fallback when its 2D mesher reports a face failed - gmsh by default meshes that face again
#: with MeshAdapt - is off (0), and the face keeps its Frontal-Delaunay mesh, checked by every step after as any other.
#: MeshAdapt hung over an hour on one 5,000-element face (d2 t0057; 19 s, valid and inside Nastran's limits with the
#: fallback off) and took 134 s of a 2,030-element part's 135 (d2 t0044). None: gmsh's own default
ALGO_SWITCH = 0
#: SHMESH-28: the last clean-up (shell_quality.mend) under the user's limits; then relax and mend again, at most
#: MEND_CYCLES times
MEND = True
MEND_CYCLES = 3
#: ... in which an edge over the user's longest edge (under their shortest) costs as an element in a band does
MEND_LONG = True
MEND_SHORT = False
#: ... and a node of an element outside a line may move to where that element is well shaped (shell_quality.mend)
MEND_MOVES = True
#: SHMESH-14d (under the user's limits): the nodes on a straight CAD edge relax too, sliding along their edge, never
#: nearer its ends than SLIDE_ENDS of its length
SLIDE_EDGES = True
SLIDE_ENDS = 0.01
#: ... but not at the corners of a mapped strip (a mapped grid cannot grade), and no strip is left free for one
FEATURE_AT_STRIPS = True
FEATURE_UNMAP = False


class _Periodic(Exception):
    """gmsh could not mesh sewn face `face` (0-based, in the surface's own order) as a periodic surface."""

    def __init__(self, face: int, *, kin: bool = False, loop: bool = False) -> None:
        super().__init__(f"face {face}")
        self.face = face
        self.kin = kin      # re-make every sewn face on the same surface with it (SHMESH-11b)
        self.loop = loop    # gmsh read the face's 1D mesh as not a closed loop (SHMESH-11c)


def _generate(gmsh, tag_of: dict[int, int]) -> None:
    """Mesh in 2D; a face gmsh cannot mesh as a periodic surface raises _Periodic for it (SHMESH-11), and so does
    an analytic face whose boundary gmsh's 2D mesher cannot recover (SHMESH-11b): the face gmsh was meshing when
    it stopped, read from its log."""
    import re

    gmsh.logger.start()
    try:
        gmsh.model.mesh.generate(2)
    except Exception as exc:
        m = re.search(r"periodic surface (\d+)", str(exc))
        if m and int(m.group(1)) in tag_of:
            raise _Periodic(tag_of[int(m.group(1))]) from exc
        text = str(exc).lower()
        # SHMESH-11c: and so does a face whose boundary's 1D mesh gmsh reads as "not a closed loop" (an analytic or
        # trimmed face on a closed surface whose loop runs across the surface's seam in its parameters: a tee's run,
        # a mitred bend's pieces; fresh fr015/fr017) - as a B-spline it has no seam. A face already a B-spline is
        # not re-made again (gmsh names it "BSpline surface"): the error stands
        if RESPLINE_UNRECOVERED and ("recover" in text or (RESPLINE_LOOP and "closed loop" in text)):
            at = [int(x) for line in gmsh.logger.get() for x in re.findall(r"Meshing surface (\d+)", line)]
            kind = gmsh.model.getType(2, at[-1]).lower() if at else ""
            if at and at[-1] in tag_of and "spline" not in kind and "bezier" not in kind:
                raise _Periodic(tag_of[at[-1]], kin="recover" in text, loop="recover" not in text) from exc
        raise


def _even_faces(gmsh, tag_of: dict[int, int], fixed: set[int], floor: dict[int, float]) -> int:
    """SHMESH-19: Blossom can pair every triangle of a face into quads only when the face's boundary has an even
    number of segments; with an odd number gmsh fell back and left hundreds of triangles (sh0019: 687 on one plate).
    After the 1D mesh, each face with an odd count gets one segment more on one of its own free curves (a rim no
    other face meshes), the most evenly divided one (a hole's circle before a graded edge), the longest count on a
    tie; a face with no free curve takes it on a curve it shares with one other odd face, which evens both. Curves
    in `fixed` (already set: mapped strips) are left, and so is a curve whose segments would come out under the floor
    of a face it bounds (`floor`, per surface; a 0.7 mm spline cut in 3 left slivers). The most evenly divided means
    to a tenth; then the most segments (the smallest change).
    The 1D mesh is then cleared, so the caller meshes again.
    Returns how many curves were given a segment more."""
    gmsh.model.mesh.generate(1)
    seg: dict[int, int] = {}
    spread: dict[int, float] = {}
    length: dict[int, float] = {}
    for _d, c in gmsh.model.getEntities(1):
        _t, _tags, nodes = gmsh.model.mesh.getElements(1, c)
        conn = np.concatenate([np.asarray(x, dtype=np.int64) for x in nodes]).reshape(-1, 2) if nodes else np.zeros((0, 2), dtype=np.int64)
        seg[c] = len(conn)
        if len(conn):
            tags, xyz, _p = gmsh.model.mesh.getNodes(1, c, includeBoundary=True)
            at = {int(t): i for i, t in enumerate(tags)}
            p = np.asarray(xyz, dtype=float).reshape(-1, 3)
            ln = np.array([np.linalg.norm(p[at[int(a)]] - p[at[int(b)]]) for a, b in conn])
            spread[c] = round(float(ln.max() / max(ln.min(), 1e-300)), 1)
            length[c] = float(ln.sum())
    faces_of: dict[int, list[int]] = {}
    bound: dict[int, list[int]] = {}
    for t in sorted(tag_of):
        bound[t] = sorted(abs(c) for _d, c in gmsh.model.getBoundary([(2, t)], oriented=False))
        for c in bound[t]:
            faces_of.setdefault(c, []).append(t)
    odd = {t for t in bound if sum(seg.get(c, 0) for c in bound[t]) % 2}
    bumped: set[int] = set()

    def ok(c: int) -> bool:
        return (c not in fixed and c not in bumped and seg.get(c, 0) > 0
                and length[c] / (seg[c] + 1) >= max(floor.get(t, 0.0) for t in faces_of.get(c, [])))

    for t in sorted(odd):
        if t not in odd:
            continue
        free = [c for c in bound[t] if ok(c) and faces_of.get(c) == [t]]
        if not free:
            continue
        c = min(free, key=lambda c: (spread.get(c, 1.0), -seg[c], c))
        bumped.add(c)
        odd.discard(t)
    for t in sorted(odd):
        if t not in odd:
            continue
        shared = [c for c in bound[t] if ok(c) and len(faces_of.get(c, [])) == 2
                  and [o for o in faces_of[c] if o != t][0] in odd]
        if not shared:
            continue
        c = min(shared, key=lambda c: (spread.get(c, 1.0), -seg[c], c))
        bumped.add(c)
        odd -= set(faces_of[c])
    for c in sorted(bumped):
        gmsh.model.mesh.setTransfiniteCurve(c, seg[c] + 2)
    if bumped:
        # (clearing only the curves given a segment more left their end nodes behind: gmsh found open loops)
        gmsh.model.mesh.clear()
    return len(bumped)


def _arc_radius(gmsh, curve: int) -> float | None:
    """The radius (model units) of a gmsh curve that is a circle or a circular arc; None for any other curve."""
    if gmsh.model.getType(1, curve) != "Circle":
        return None
    lo, hi = gmsh.model.getParametrizationBounds(1, curve)
    k = gmsh.model.getCurvature(1, curve, [0.5 * (lo[0] + hi[0])])
    return 1.0 / float(k[0]) if len(k) and float(k[0]) > 0 else None


def _map_strips(gmsh, tag_of: dict[int, int], rows: list[dict], mm: float,
                sizing: dict | None = None, fine_at: dict[int, float] | None = None
                ) -> dict[int, tuple[tuple[int, ...], float]]:
    """SHMESH-16: every narrow four-sided strip (a bend, a flange, a strip between holes: its two long sides alike in
    length, its two ends alike, the long sides at least STRIP_LONG times the ends) is meshed as a mapped grid of
    quads - its planned size across it, and along it the size of the faces beside its long sides, at most
    STRETCH_MAX times the size across. A floor-sized bend no longer fills its length with floor-sized elements, and
    a mapped grid leaves no triangles. Returns {strip surface: (its long sides, the size along them)}. Counts are set once per curve: a
    strip whose curve another strip has already counted differently stays free (meshed as before).
    `sizing` (SHMESH-24, the user's sizes): the stretch is held under their aspect ratio and their longest edge,
    and a count is not made even when that would put its segments under their shortest edge."""
    count: dict[int, int] = {}
    low = (sizing or {}).get("low_mm")
    high = (sizing or {}).get("high_mm")

    def even(n: int, length: float) -> int:
        if not n % 2 or (low is not None and length * mm / (n + 1) < low):
            return n
        return n + 1

    out: dict[int, tuple[tuple[int, ...], float]] = {}
    size_of = {t: rows[k]["size_mm"] / mm for t, k in tag_of.items()}
    for t, k in sorted(tag_of.items()):
        flat = gmsh.model.getType(2, t) == "Plane"
        # SHMESH-27: under the user's sizes a flat strip narrower than its size (one element across) is mapped too
        user_flat = flat and sizing is not None and FEATURE_SIZES
        if (rows[k]["rule"] == "SHMESH-15" or (flat and not MAP_FLAT and not user_flat)
                or len(gmsh.model.getBoundary([(2, t)], oriented=False)) != 4):
            continue
        sides = sorted({abs(c) for _d, c in gmsh.model.getBoundary([(2, t)], oriented=False)})
        if len(sides) != 4:
            continue
        ends = {c: {abs(p) for _d, p in gmsh.model.getBoundary([(1, c)], oriented=False)} for c in sides}
        # the side sharing no corner with the first is its opposite; the other two face each other
        opposite = [c for c in sides[1:] if not (ends[c] & ends[sides[0]])]
        if len(opposite) != 1:
            continue
        a = (sides[0], opposite[0])
        b = tuple(c for c in sides[1:] if c != opposite[0])
        if ends[b[0]] & ends[b[1]]:
            continue
        la = tuple(float(gmsh.model.occ.getMass(1, c)) for c in a)
        lb = tuple(float(gmsh.model.occ.getMass(1, c)) for c in b)
        if abs(la[0] - la[1]) > STRIP_ALIKE * max(la) or abs(lb[0] - lb[1]) > STRIP_ALIKE * max(lb):
            continue
        if max(la) < max(lb):
            a, b, la, lb = b, a, lb, la
        if min(la) < STRIP_LONG * max(lb) or max(lb) * mm >= plan_rules.PANEL_WALLS * rows[k]["wall_mm"]:
            continue
        across = size_of[t]
        if user_flat and not MAP_FLAT and max(lb) >= across:
            continue
        beside = [size_of[s] for c in a for s in gmsh.model.getAdjacencies(1, c)[0] if s != t and s in size_of]
        stretch = STRETCH_FLAT if flat and not user_flat else STRETCH_MAX
        if sizing is not None and sizing.get("stretch") is not None:
            stretch = min(stretch, max(1.0, float(sizing["stretch"])))
        along = min([stretch * across] + beside)
        if sizing is None:
            along = max(along, across)
        else:
            # SHMESH-24: a strip narrower than the user's size has one element across it, as wide as the strip:
            # its length is held to the user's aspect ratio of THAT width, and under their longest edge
            seg = max(lb) / max(1, math.ceil(max(lb) / across - 1e-9))
            if sizing.get("stretch") is not None and SR.STRIP_ASPECT:
                along = min(along, max(1.0, float(sizing["stretch"])) * seg)
            if high is not None:
                along = min(along, 0.98 * high / mm)
            along = max(along, min(across, seg))
        if fine_at:
            # SHMESH-27: a mapped grid cannot grade. A strip with a smaller piece of the CAD at one of its corners
            # (sized more than one growth step under the strip's own elements) stays free, and grades down to it:
            # mapped, a tube corner's 10 mm elements fanned out of a 0.5 mm face at its end (sh0110)
            near = min((fine_at.get(p, math.inf) for c in sides for p in ends[c]), default=math.inf)
            if near * plan_rules.GROWTH < min(along, max(lb) / max(1, math.ceil(max(lb) / across - 1e-9))) * mm:
                continue
        n_b = max(1, math.ceil(max(lb) / across - 1e-9))
        n_a = max(1, math.ceil(max(la) / along - 1e-9))
        n_a = count.get(a[0], count.get(a[1], n_a))
        n_b = count.get(b[0], count.get(b[1], n_b))
        if EVEN:
            n_a, n_b = even(n_a, max(la)), even(n_b, max(lb))
        if any(count.get(c, n_a) != n_a for c in a) or any(count.get(c, n_b) != n_b for c in b):
            continue
        for c in a:
            count[c] = n_a
        for c in b:
            count[c] = n_b
        out[t] = (tuple(a), max(la) / n_a)
    for c, n in sorted(count.items()):
        gmsh.model.mesh.setTransfiniteCurve(c, n + 1)
    for t in sorted(out):
        gmsh.model.mesh.setTransfiniteSurface(t)
        gmsh.model.mesh.setRecombine(2, t)
    return out


def _rebuild(brep: Path, owners: list[str], out: Path, change) -> list[str]:
    """The sewn surface with some faces replaced: change(k, face) gives the faces that stand for face k (None:
    unchanged). The new faces and the rest are fused back into one conformal surface (a neighbour's edge is cut
    where a new corner lies on it), written to `out`; returns the owners, one per face of it."""
    from OCP.BOPAlgo import BOPAlgo_Builder
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS, TopoDS_Shape
    from OCP.TopTools import TopTools_IndexedMapOfShape

    from meshpipeline.cad import midsurface as ms

    shape = TopoDS_Shape()
    if not BRepTools.Read_s(shape, str(brep), BRep_Builder()):
        raise ValueError("the mid-surface could not be read")
    before = ms._faces(shape)
    if len(before) != len(owners):
        raise ValueError(f"the mid-surface has {len(before)} faces but the record names {len(owners)}")
    pieces: list[tuple[object, str]] = []
    changed = False
    for k, (f, own) in enumerate(zip(before, owners, strict=True)):
        got = change(k, f)
        changed = changed or got is not None
        pieces += [(g, own) for g in (got if got is not None else [f])]
    if not changed:
        BRepTools.Write_s(shape, str(out))
        return list(owners)
    if len(pieces) == 1:
        BRepTools.Write_s(pieces[0][0], str(out))
        return [pieces[0][1]]
    def fuse(fuzzy: float):
        b = BOPAlgo_Builder()
        for g, _own in pieces:
            b.AddArgument(g)
        b.SetRunParallel(False)
        b.SetNonDestructive(True)
        if fuzzy > 0:
            b.SetFuzzyValue(fuzzy)
        b.Perform()
        return b

    bop = fuse(0.0)
    if bop.HasErrors():
        # SHMESH-11d: once more with a fuzzy value of twice the surface's own largest tolerance (its sewn edges'
        # gaps, measured): an exact fuse of a duct's pieces failed outright (fresh fr015)
        bop = fuse(2.0 * max(ms._max_tol(shape), 1e-7))
    if bop.HasErrors():
        raise ValueError("the re-made faces of the mid-surface could not be fused back")
    fused = bop.Shape()
    after = ms._faces(fused)
    index = TopTools_IndexedMapOfShape()
    for f in after:
        index.Add(f)
    owner_of: dict[int, str] = {}
    for g, own in pieces:
        images = [TopoDS.Face_s(x) for x in bop.Modified(g)] or ([] if bop.IsDeleted(g) else [g])
        for x in images:
            k = index.FindIndex(x)
            if k > 0:
                owner_of.setdefault(k, own)
    if len(owner_of) != len(after):
        raise ValueError("a re-made face of the mid-surface could not be traced to its mid-face")
    BRepTools.Write_s(fused, str(out))
    return [owner_of[k] for k in range(1, len(after) + 1)]


def split_closed(brep: Path, owners: list[str], out: Path) -> list[str]:
    """SHMESH-11: every sewn face closed on itself (a full ring, a torus, a sphere: a seam in it) is cut at its seams
    into open pieces. gmsh cannot mesh some closed faces ("impossible to mesh periodic surface")."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepBuilderAPI import BRepBuilderAPI_NurbsConvert
    from OCP.Geom import Geom_OffsetSurface
    from OCP.ShapeUpgrade import ShapeUpgrade_ShapeDivideClosed

    from meshpipeline.cad import midsurface as ms

    def cut(_k, f):
        # SHMESH-11g: a face on an offset surface (a drafted wall's mid-face) is re-made as a B-spline first: cut at
        # its seam as an offset it came apart into invalid halves that the fuse dropped (fresh fr018: the whole cone
        # gone, a mesh of 0 elements), and gmsh reads offset surfaces as "Unknown" anyway
        remade = None
        if OFFSETS_AS_SPLINES and isinstance(BRep_Tool.Surface_s(f), Geom_OffsetSurface):
            got = ms._faces(BRepBuilderAPI_NurbsConvert(f, True).Shape())
            if len(got) == 1:
                remade = f = got[0]
        tool = ShapeUpgrade_ShapeDivideClosed(f)
        tool.SetNbSplitPoints(1)
        tool.Perform()
        got = ms._faces(tool.Result())
        return got if len(got) > 1 else ([remade] if remade is not None else None)

    return _rebuild(brep, owners, out, cut)


def respline(brep: Path, owners: list[str], face: int, out: Path, *, kin: bool = False) -> tuple[list[str], int]:
    """SHMESH-11: sewn face `face` re-made as a B-spline surface (exactly the same surface, without its period);
    with `kin`, every sewn face that lies on the same surface too (SHMESH-11b: a hull cylinder's four slotted halves
    each stopped gmsh in turn, a whole mesh each). Returns (the owners, how many faces were re-made)."""
    from OCP.BRep import BRep_Builder, BRep_Tool
    from OCP.BRepBuilderAPI import BRepBuilderAPI_NurbsConvert
    from OCP.BRepTools import BRepTools
    from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
    from OCP.gp import gp_Pnt
    from OCP.TopoDS import TopoDS_Shape

    from meshpipeline.cad import midsurface as ms

    def _on(surf, p, tol):
        pr = GeomAPI_ProjectPointOnSurf(gp_Pnt(float(p[0]), float(p[1]), float(p[2])), surf)
        return pr.NbPoints() > 0 and pr.LowerDistance() <= tol

    picked = {face}
    if kin:
        shape = TopoDS_Shape()
        BRepTools.Read_s(shape, str(brep), BRep_Builder())
        faces = ms._faces(shape)
        base = faces[face]
        kind = _surface_type(base)
        surf = BRep_Tool.Surface_s(base)
        tol = max(1e-6, 1e-6 * float(np.linalg.norm(np.subtract(*ms._box(base)))))
        for k, f in enumerate(faces):
            if k in picked or _surface_type(f) != kind:
                continue
            # on the same surface: points read inside the face lie on the failed face's surface
            pts = [ms._np(q) for q in _probe_points(f)]
            if pts and all(_on(surf, p, tol) for p in pts):
                picked.add(k)

    def convert(k, f):
        if k not in picked:
            return None
        nc = BRepBuilderAPI_NurbsConvert(f, True)
        return ms._faces(nc.Shape())

    return _rebuild(brep, owners, out, convert), len(picked)


def _surface_type(face):
    """The face's surface type (OpenCASCADE GeomAbs)."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface

    return BRepAdaptor_Surface(face).GetType()


def _probe_points(face) -> list:
    """Three points of the face: its centre of mass laid on it, and two of its vertices."""
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_VERTEX
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    out: list = []
    ex = TopExp_Explorer(face, TopAbs_VERTEX)
    while ex.More() and len(out) < 3:
        out.append(BRep_Tool.Pnt_s(TopoDS.Vertex_s(ex.Current())))
        ex.Next()
    return out


def _faces_and_facts(brep: Path, mm: float):
    """The sewn faces (OpenCASCADE MapShapes order), each with its centroid, width (mm) and curve radii (mm)."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepTools import BRepTools
    from OCP.GeomAbs import GeomAbs_Circle
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS, TopoDS_Shape

    from meshpipeline.cad import fea_measure as fm
    from meshpipeline.cad import midsurface as ms

    shape = TopoDS_Shape()
    if not BRepTools.Read_s(shape, str(brep), BRep_Builder()):
        raise ValueError("the mid-surface could not be read")
    out = []
    for f in ms._faces(shape):
        g = GProp_GProps()
        BRepGProp.SurfaceProperties_s(f, g)
        c = g.CentreOfMass()
        radii = []
        ex = TopExp_Explorer(f, TopAbs_EDGE)
        while ex.More():
            e = TopoDS.Edge_s(ex.Current())
            a = BRepAdaptor_Curve(e)
            if a.GetType() == GeomAbs_Circle:
                radii.append(a.Circle().Radius() * mm)
            ex.Next()
        out.append({"centroid": np.array([c.X(), c.Y(), c.Z()]), "area": g.Mass(), "width": ms.strip_width(f) * mm,
                    "box_width": ms.width_of(f) * mm, "radii": radii, "bend": fm.face_radius(f, mm),
                    "normal_face": f})
    return out


def _sheet_widths(brep: Path, facts: list[dict], owners: list[str], record: dict, mm: float) -> dict:
    """SHMESH-30: each sewn face's region (faces of one wall joined where the surface runs on smoothly,
    shell_plan.smooth_link) and that region's width, measured as one face's is (mm: its oriented box's middle side
    or twice its area over its own boundary, the narrower). The 400 x 240 mm panel of the patch study: 1,979 elements
    as 1 patch, and 2,005 / 2,178 / 2,513 as 4 / 12 / 30 (patch by patch: 7,937 / 22,468 / 2,523). Returns
    {"widths": per face (0 for a face alone), "size": faces in its region}."""
    from OCP.Bnd import Bnd_OBB
    from OCP.BRep import BRep_Builder
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS, TopoDS_Shape
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    from meshpipeline.cad import midsurface as ms

    shape = TopoDS_Shape()
    if not BRepTools.Read_s(shape, str(brep), BRep_Builder()):
        raise ValueError("the mid-surface could not be read")
    faces = ms._faces(shape)
    fmap = ms._map(shape, TopAbs_FACE)          # the same order as `faces` (1-based)
    mids = {m["id"]: m for m in record["mids"]}
    pairs = {p["id"]: p for p in record["pairs"]}

    def wall_of(k: int) -> float:
        return float(pairs[mids[owners[k]]["pair"]]["thickness_mm"])

    emap = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, emap)
    links: list[tuple[int, int]] = []
    for i in range(1, emap.Extent() + 1):
        ks = sorted({fmap.FindIndex(x) - 1 for x in emap.FindFromIndex(i)} - {-1})
        if len(ks) != 2:
            continue
        a, b = ks
        same = abs(wall_of(a) - wall_of(b)) <= 1e-6 * max(1.0, wall_of(a)) and (
            mids[owners[a]]["pair"] == mids[owners[b]]["pair"] or mids[owners[a]].get("body") == mids[owners[b]].get("body"))
        if not same:
            continue
        c = BRepAdaptor_Curve(TopoDS.Edge_s(emap.FindKey(i)))
        p = c.Value(0.5 * (c.FirstParameter() + c.LastParameter()))
        na = ms._face_normal_at(faces[a], (p.X(), p.Y(), p.Z()))
        nb = ms._face_normal_at(faces[b], (p.X(), p.Y(), p.Z()))
        if na is None or nb is None:
            continue
        cosv = abs(float(np.dot(na, nb))) / max(float(np.linalg.norm(na) * np.linalg.norm(nb)), 1e-300)
        angle = math.degrees(math.acos(min(1.0, cosv)))
        if plan_rules.smooth_link(angle, same_wall=True, bends=(facts[a]["bend"], facts[b]["bend"]),
                                  wall=min(wall_of(a), wall_of(b))):
            links.append((a, b))
    region = plan_rules.sheet_regions(len(faces), links)
    members: dict[int, list[int]] = {}
    for k, r in enumerate(region):
        members.setdefault(r, []).append(k)
    widths = [0.0] * len(faces)
    size = [1] * len(faces)
    # each edge's faces and length, for the regions' own boundaries
    edge_faces = []
    for i in range(1, emap.Extent() + 1):
        ks = sorted({fmap.FindIndex(x) - 1 for x in emap.FindFromIndex(i)} - {-1})
        edge_faces.append((ks, ms._length(emap.FindKey(i))))
    for ks in members.values():
        if len(ks) < 2:
            continue
        # the region measured as one face is: the narrower of its oriented box's middle side and twice its area
        # over its own boundary (the edges only one of its faces holds, holes included) - midsurface.strip_width
        inside = set(ks)
        area = sum(ms._area(faces[k]) for k in ks)
        rim = sum(length for fs, length in edge_faces if sum(1 for f in fs if f in inside) == 1)
        obb = Bnd_OBB()
        BRepBndLib.AddOBB_s(ms._compound([faces[k] for k in ks]), obb, True, True, False)
        w = sorted(2.0 * x for x in (obb.XHSize(), obb.YHSize(), obb.ZHSize()))[1]
        if rim > 0:
            w = min(w, 2.0 * area / rim)
        for k in ks:
            widths[k], size[k] = w * mm, len(ks)
    return {"widths": widths, "size": size}


def _reference_normal(face) -> np.ndarray | None:
    """The face's oriented normal at its middle (the side its sheet's normals are turned to)."""
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    from meshpipeline.cad import midsurface as ms

    g = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, g)
    c = g.CentreOfMass()
    n = ms._face_normal_at(face, np.array([c.X(), c.Y(), c.Z()]))
    if n is None:
        return None
    from OCP.TopAbs import TopAbs_REVERSED

    return -n if face.Orientation() == TopAbs_REVERSED else n


def mesh_shell(brep: Path | str, record: dict, *, mm_per_unit: float = 1.0, log: list | None = None,
               target: dict | None = None, correction: float = 1.0,
               requirements: dict | None = None) -> tuple[ShellMesh, dict]:
    """Mesh the sewn mid-surface: (the shell mesh in millimetres, the plan as applied: one row per face).
    `target`: the user's element count (SHMESH-23, fea_driver.element_target); `correction`: the built-over-planned a
    first mesh measured (mesh_run aims again with it). `requirements`: the mesh requirements read from the user's
    notes (engines/mesh_requirements; SHMESH-24..26) - None: the rules' own mesh, exactly as before.

    SHMESH-21: run in a forked child stopped after MESH_DEADLINE_S (ValueError, in words): gmsh can hang on a face
    with no way back to Python (fresh fr017's re-made faces: over 20 minutes on one 2D mesh), and a hung mesh must
    fail fast for the solid mesh to be delivered. Without fork (not Linux), or with no deadline, it runs here."""
    import multiprocessing as mp
    import os

    if MESH_DEADLINE_S <= 0 or "fork" not in mp.get_all_start_methods():
        return _mesh_shell(brep, record, mm_per_unit=mm_per_unit, log=log, target=target, correction=correction,
                           requirements=requirements)
    recv, send = mp.get_context("fork").Pipe(duplex=False)

    def child() -> None:
        try:
            got: list = []
            mesh, applied = _mesh_shell(brep, record, mm_per_unit=mm_per_unit, log=got, target=target,
                                        correction=correction, requirements=requirements)
            send.send(("ok", (mesh, applied), got))
        except BaseException as exc:  # noqa: BLE001 - told to the parent, which raises it there
            send.send(("error", f"{type(exc).__name__}: {exc}", None))
        finally:
            send.close()
            os._exit(0)

    proc = mp.get_context("fork").Process(target=child, daemon=True)
    proc.start()
    send.close()
    try:
        if not recv.poll(MESH_DEADLINE_S):
            raise ValueError(f"the shell mesh ran past {MESH_DEADLINE_S / 60:.0f} minutes and was stopped (SHMESH-21)")
        status, out, got = recv.recv()
    finally:
        if proc.is_alive():
            proc.kill()
        proc.join()
        recv.close()
    if status != "ok":
        raise ValueError(out)
    if log is not None:
        log.extend(got)
    return out


def _mesh_shell(brep: Path | str, record: dict, *, mm_per_unit: float = 1.0, log: list | None = None,
                target: dict | None = None, correction: float = 1.0,
                requirements: dict | None = None) -> tuple[ShellMesh, dict]:
    """mesh_shell, unbounded, in this process."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="shell_mesh_") as tmp:
        owners = list(record["surface_faces"])
        split_note = ""
        if SPLIT_CLOSED:
            cut = Path(tmp) / "cut.brep"
            try:
                owners = split_closed(Path(brep), owners, cut)
                brep = cut
            except ValueError as exc:
                # SHMESH-11d: the closed faces' pieces would not fuse back with the rest (a 45 degree cone's pieces,
                # exact or fuzzy): the surface is meshed as sewn, and a closed face gmsh stops on is re-made below
                split_note = f"the closed faces were meshed whole ({exc})"
        resplined = loops = 0
        started = time.monotonic()
        while True:
            try:
                mesh, applied = _mesh_once(Path(brep), record, owners, float(mm_per_unit), log, target=target,
                                           correction=correction, requirements=requirements)
            except _Periodic as p:
                if resplined >= RESPLINE_MAX:
                    raise ValueError(f"gmsh could not mesh {resplined + 1} periodic faces") from p
                loops += p.loop
                if loops > RESPLINE_LOOP_MAX:
                    # SHMESH-11c: a surface whose faces fail one after another is not a seam to take out: it stops
                    raise ValueError(f"gmsh could not close the boundary of {loops} faces in turn, each re-made as a "
                                     "B-spline; the shell mesh is given up (SHMESH-11c)") from p
                # SHMESH-11e: each re-made face costs a whole mesh again; past the budget the mesh stops and says why
                spent = time.monotonic() - started
                if spent > RESPLINE_BUDGET_S:
                    raise ValueError(f"gmsh stopped on a face again after {resplined} face(s) re-made and "
                                     f"{spent / 60:.0f} minutes; the shell mesh is given up (SHMESH-11e)") from p
                nxt = Path(tmp) / f"respline_{resplined}.brep"
                owners, made = respline(Path(brep), owners, p.face, nxt, kin=p.kin)
                brep = nxt
                resplined += made
                continue
            past = applied.pop("_past_spots", [])
            past_n = applied.pop("_past_n", 0)
            own: list = []
            for _ in range(OWN_SIZE_RETRIES if requirements else 0):
                # SHMESH-31: elements past the solver's own limits under the user's sizes: round each of them the
                # mesh is made again at our rules' own size for its face (finer), graded back to the user's; the
                # mesh with fewer past is kept
                if not past:
                    break
                own = own + past
                try:
                    again, applied2 = _mesh_once(Path(brep), record, owners, float(mm_per_unit), None,
                                                 target=target, correction=correction, requirements=requirements,
                                                 own_spots=tuple(own))
                except (ValueError, _Periodic) as exc:
                    logger.info("shell mesh: the spots past the solver's limits were not meshed again (%s)", exc)
                    break
                past2 = applied2.pop("_past_spots", [])
                n2 = applied2.pop("_past_n", 0)
                if n2 >= past_n:
                    break
                mesh, applied, past, past_n = again, applied2, past2, n2
                applied["quality"]["own_size_spots"] = len(own)
            if applied["quality"].get("own_size_spots"):
                mesh.notes.append(f"In {applied['quality']['own_size_spots']} spot(s) the mesh is finer than the size "
                                  "you asked for: at your size some elements there were past the solver's own limits "
                                  "(SHMESH-31).")
            applied["quality"]["resplined_faces"] = resplined
            if split_note:
                mesh.notes.append(f"{split_note[0].upper()}{split_note[1:]} (SHMESH-11d).")
            if resplined:
                mesh.notes.append(f"{resplined} face(s) gmsh could not mesh (a periodic surface, or a boundary it "
                                  "could not recover) were re-made as B-splines (the same surface) and meshed "
                                  "(SHMESH-11).")
            return mesh, applied


def _mesh_once(brep: Path, record: dict, owners: list[str], mm: float, log: list | None, *,
               target: dict | None = None, correction: float = 1.0,
               requirements: dict | None = None, own_spots: tuple = ()) -> tuple[ShellMesh, dict]:
    """One mesh. `own_spots` (SHMESH-31): (sewn face, box low corner, box high corner - mm) round elements a mesh
    under the user's sizes left past the solver's own limits: inside each box the face's own rules' size, where
    finer than the user's, graded back out."""
    import gmsh

    facts = _faces_and_facts(Path(brep), mm)
    if len(owners) != len(facts):
        raise ValueError(f"the mid-surface has {len(facts)} faces but the record names {len(owners)}")
    rows = []
    widths = [min(f["width"], f["box_width"]) if f["box_width"] > 0 else f["width"] for f in facts]
    sheet = None
    if SHEET_REGIONS_ALL or (SHEET_REGIONS and record.get("source") == "surface"):
        # SHMESH-30: a sheet drawn as many patches is sized by the sheet
        sheet = _sheet_widths(Path(brep), facts, owners, record, mm)
        widths = [max(w, s) for w, s in zip(widths, sheet["widths"], strict=True)]
    for k, f in enumerate(facts):
        rows.append({"face": k + 1, **plan_rules.face_plan(record, owners[k], width=widths[k], radii=f["radii"],
                                                         bend=f["bend"])})
        if sheet is not None and sheet["size"][k] > 1:
            rows[-1]["sheet_mm"] = float(f"{sheet['widths'][k]:.6g}")
    walls = [r["wall_mm"] for r in rows]
    # SHMESH-20: a plan far past what a shell mesh is made in within the time allowed stops before gmsh starts, and
    # says so (a ship's 3 m slotted hull, sh0048: about 3 million planned, over an hour of gmsh)
    planned = sum(f["area"] * mm * mm / r["size_mm"] ** 2 for f, r in zip(facts, rows, strict=True))
    # SHMESH-23: THE USER'S ELEMENT COUNT scales every size by one factor (their count beats our minimums: said, never
    # refused), aimed on the built-over-planned a first mesh measured
    counted, sz = _count_plan(rows, planned, target, correction)
    if counted is not None:
        planned = counted["planned"]
    # SHMESH-24..26: THE USER'S REQUIREMENTS from their notes (engines/shell_requirements). Their sizes replace the
    # plan's (their size beats our minimums: said, never refused; only a plan too big to build is stepped to the
    # nearest that builds), their limits join the limits in force, their element kind is made. None: nothing here
    # changes a number - `lim` is shell_quality.IN_FORCE, `sizing` and `kind` are None
    lim = SR.limits_in_force(requirements)
    kind = SR.kind_of(requirements)
    rule_size = {k: min(rows[k]["size_mm"], rows[k].get("curve_mm", rows[k]["size_mm"])) for k, _lo, _hi in own_spots}
    sizing = SR.size_plan(rows, requirements, [f["area"] * mm * mm for f in facts], planned_max=PLANNED_MAX,
                          curved=[(f["bend"], min(f["width"], f["box_width"]) if f["box_width"] > 0 else f["width"])
                                  for f in facts])
    if sizing is not None:
        planned = sizing["planned"]
    # SHMESH-27 (the user's sizes only): a face narrower than their size is sized to its own width
    narrow = (_narrow_faces(rows, facts, mm, planned, _interior_faces(Path(brep)) if FLANKS else None)
              if sizing is not None and FEATURE_SIZES else None)
    if narrow is not None:
        planned = narrow["planned"]
    finest = (sizing or {}).get("floor_mm")
    growth = float((sizing or {}).get("growth") or plan_rules.GROWTH)
    if planned > PLANNED_MAX:
        raise ValueError(f"the shell mesh plan asks for about {planned / 1e6:.1f} million elements over {len(facts)} "
                         f"faces, past the {PLANNED_MAX / 1e6:.1f} million a shell mesh is made of in the time allowed "
                         "(SHMESH-20)")
    gmsh.initialize(["-noenv"], readConfigFiles=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("General.NumThreads", 1)
        gmsh.option.setNumber("Geometry.OCCParallel", 0)
        gmsh.option.setNumber("Mesh.Algorithm", ALGORITHM)
        gmsh.option.setNumber("Mesh.RecombineAll", 0 if kind == "triangles" else 1)
        gmsh.option.setNumber("Mesh.RecombinationAlgorithm", RECOMBINATION)
        # (with the user's own sizes, SHMESH-24, every curved face and arc is sized by the plan itself, between
        # the user's shortest edge and the shape it must keep: gmsh's own curvature sizing would refine past both)
        # SHMESH-33: a count the user asked for that makes the sizes coarser than ours (SHMESH-23, sz > 1) coarsens
        # the elements round every curve too, by the same factor, never under SHAPE_PER_TURN a turn: gmsh's own
        # 24 a turn held every 12 mm hole and every curved panel at our size (a bracket asked 221 elements got 503,
        # a curved panel asked 1,263 got 21,256)
        per_turn = PER_TURN if sz <= 1.0 else max(SR.SHAPE_PER_TURN, int(round(PER_TURN / sz)))
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", per_turn if finest is None else 0)
        # SHMESH-6 is held by the grading fields below; gmsh's own spread of edge sizes into a face carried a
        # narrow neighbour's size over the whole of a wide face (one 2.5 mm strip: 800,000 elements)
        gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 0)
        gmsh.option.setNumber("Mesh.MeshSizeFromPoints", 0)
        gmsh.option.setNumber("Mesh.MeshSizeMin", plan_rules.FLOOR_WALLS * min(walls) * min(sz, 1.0) / mm)
        gmsh.option.setNumber("Mesh.MeshSizeMax", max(r["size_mm"] for r in rows) / mm)
        gmsh.option.setNumber("Mesh.Smoothing", 5)
        gmsh.option.setNumber("Mesh.LcIntegrationPrecision", LC_PRECISION)
        if ALGO_SWITCH is not None:
            gmsh.option.setNumber("Mesh.AlgorithmSwitchOnFailure", ALGO_SWITCH)
        gmsh.model.add("shell")
        gmsh.model.occ.importShapes(str(brep), highestDimOnly=False)
        gmsh.model.occ.synchronize()
        surfaces = [t for _d, t in gmsh.model.getEntities(2)]
        if len(surfaces) != len(facts):
            raise ValueError(f"gmsh read {len(surfaces)} faces of the mid-surface's {len(facts)}")
        # each gmsh surface to its sewn face, by centroid and area (never by trusting the order); faces alike in
        # both (a cone's apex slivers of 1e-6 mm2, all at one point) go in order, each sewn face once
        cents = np.array([f["centroid"] for f in facts])
        areas = np.array([f["area"] for f in facts])
        scale = 1e-9 * max(1.0, float(np.abs(cents).max()) if len(cents) else 1.0)
        tag_of: dict[int, int] = {}
        free = np.ones(len(facts), dtype=bool)
        for t in sorted(surfaces):
            c = np.array(gmsh.model.occ.getCenterOfMass(2, t))
            a = float(gmsh.model.occ.getMass(2, t))
            dist = np.where(free, np.linalg.norm(cents - c, axis=1), np.inf)
            best = float(dist.min())
            if not np.isfinite(best):
                raise ValueError("gmsh read more faces than the mid-surface has")
            near = np.flatnonzero(dist <= best + scale)
            k = int(near[np.argmin(np.abs(areas[near] - a))])
            free[k] = False
            tag_of[t] = k
        # SHMESH-12: a sewn face smaller than a square a fifth of the floor across (a cone's apex slivers of 1e-6
        # mm2) is no wall: it is taken out before meshing (gmsh stopped at one, "the 1D mesh seems not to be
        # forming a closed loop"); its edges stay, so its neighbours mesh up to them
        tiny = sorted(t for t, k in tag_of.items()
                      if facts[k]["area"] * mm * mm < (shell_mesh.SHORT_SHARE * plan_rules.FLOOR_WALLS
                                                       * rows[k]["wall_mm"]) ** 2)
        if tiny:
            gmsh.model.occ.remove([(2, t) for t in tiny])
            gmsh.model.occ.synchronize()
            for t in tiny:
                del tag_of[t]
        slivers = len(tiny)
        # SHMESH-1/2: a constant size per face; SHMESH-3/4/6: curves sized by curvature, grown at GROWTH
        fields = []
        top = max(rows[k]["size_mm"] for k in tag_of.values()) / mm

        def grow(curves: list[int], h0: float) -> None:
            """SHMESH-6: size h0 on these curves, growing by GROWTH per element up to the largest size in the
            part - never capped at a smaller one, which would hold every coarser face to it."""
            if h0 >= top or not curves:
                return
            dist = gmsh.model.mesh.field.add("Distance")
            gmsh.model.mesh.field.setNumbers(dist, "CurvesList", sorted(set(curves)))
            gmsh.model.mesh.field.setNumber(dist, "Sampling", 50)
            thr = gmsh.model.mesh.field.add("Threshold")
            gmsh.model.mesh.field.setNumber(thr, "InField", dist)
            gmsh.model.mesh.field.setNumber(thr, "SizeMin", h0)
            gmsh.model.mesh.field.setNumber(thr, "SizeMax", top)
            gmsh.model.mesh.field.setNumber(thr, "DistMin", 0.0)
            gmsh.model.mesh.field.setNumber(thr, "DistMax", (top - h0) / (growth - 1.0))
            fields.append(thr)

        # SHMESH-27: an edge of the CAD shorter than the size of the faces it bounds is graded down to
        short_edges = _short_edges(gmsh, tag_of, rows, mm) if narrow is not None else None
        # SHMESH-16: narrow four-sided strips (bends, flanges) are mapped, their elements stretched along them
        mapped = (_map_strips(gmsh, tag_of, rows, mm, sizing,
                              fine_at=short_edges["fine_at"] if short_edges and FEATURE_UNMAP else None)
                  if MAP_STRIPS and kind != "triangles" else {})
        if short_edges is not None and mapped and not FEATURE_AT_STRIPS:
            _spare_strip_corners(gmsh, tag_of, rows, mapped, narrow, short_edges)
        # SHMESH-6: a face finer than the coarsest grades its neighbours from its own edges (a mapped strip's long
        # sides from its size along them)
        rims: dict[float, list[int]] = {}
        for t, k in sorted(tag_of.items()):
            long_sides, along = mapped.get(t, ((), 0.0))
            rims.setdefault(float(rows[k]["size_mm"]), []).extend(
                abs(c) for _d, c in gmsh.model.getBoundary([(2, t)], oriented=False) if abs(c) not in long_sides)
            if long_sides:
                rims.setdefault(float(f"{along * mm:.6g}"), []).extend(long_sides)
        # one field per size, not per face: gmsh reads every field of the Min at every point it sizes, and a ship's
        # 5,392 faces made 5,392 constant fields (sh0048: its 1D mesh alone ran past 30 minutes). The same sizes.
        const_by: dict[float, list[int]] = {}
        arcs_by: dict[float, list[int]] = {}
        for t, k in sorted(tag_of.items()):
            const_by.setdefault(float(rows[k]["size_mm"]), []).append(t)
            if "curve_mm" in rows[k]:
                # SHMESH-3: each arc of the face refined to its OWN radius's size, then grown; its straight edges
                # and wider arcs keep the face's size (one small hole no longer refines the whole rim)
                for _d, c in gmsh.model.getBoundary([(2, t)], oriented=False):
                    r = _arc_radius(gmsh, abs(c))
                    if r is None:
                        continue
                    h0, _rule = plan_rules.curve_size(r * mm, wall=rows[k]["wall_mm"])
                    h0 *= sz
                    if finest is not None and sizing is not None:
                        # SHMESH-24: the user's global size (else no finer than their shortest edge allows) - but a
                        # hole keeps its shape
                        h0 = SR.curved_size(r * mm, 0.0, floor=finest, rule=(
                            rows[k]["size_mm"] if sizing.get("global_mm") is not None else h0))
                    if h0 < rows[k]["size_mm"]:
                        arcs_by.setdefault(float(f"{h0:.6g}"), []).append(abs(c))
        if short_edges is not None:
            for h, curves in short_edges["by_size"].items():
                arcs_by.setdefault(h, []).extend(curves)
        for h_mm, surfs in sorted(const_by.items()):
            fid = gmsh.model.mesh.field.add("Constant")
            gmsh.model.mesh.field.setNumber(fid, "VIn", h_mm / mm)
            gmsh.model.mesh.field.setNumbers(fid, "SurfacesList", surfs)
            gmsh.model.mesh.field.setNumber(fid, "IncludeBoundary", 1)
            fields.append(fid)
        grading: dict[float, list[int]] = {}
        for h, curves in list(rims.items()) + list(arcs_by.items()):
            grading.setdefault(h, []).extend(curves)
        if len(grading) > GROW_GROUPS:
            # gmsh reads every grading field at every point it sizes: a ship's 221 of them held its 1D mesh past 30
            # minutes. Past GROW_GROUPS sizes, each size is taken down to its step of GROW_STEP (finer, never coarser)
            binned: dict[float, list[int]] = {}
            for h, curves in grading.items():
                b = float(f"{GROW_STEP ** math.floor(math.log(h) / math.log(GROW_STEP) + 1e-9):.6g}")
                binned.setdefault(b, []).extend(curves)
            grading = binned
        for h, curves in sorted(grading.items()):
            grow(curves, h / mm)
        for k, lo, hi in own_spots:
            # SHMESH-31: the face's own rules' size inside a box round elements past the solver's limits, graded out
            h0 = rule_size[k]
            here = min(rows[k]["size_mm"], rows[k].get("curve_mm", rows[k]["size_mm"]))
            if h0 >= here:
                continue
            box = gmsh.model.mesh.field.add("Box")
            gmsh.model.mesh.field.setNumber(box, "VIn", h0 / mm)
            gmsh.model.mesh.field.setNumber(box, "VOut", top)
            for name, v in zip(("XMin", "YMin", "ZMin"), lo, strict=True):
                gmsh.model.mesh.field.setNumber(box, name, float(v) / mm)
            for name, v in zip(("XMax", "YMax", "ZMax"), hi, strict=True):
                gmsh.model.mesh.field.setNumber(box, name, float(v) / mm)
            gmsh.model.mesh.field.setNumber(box, "Thickness", (here - h0) / (growth - 1.0) / mm)
            fields.append(box)
        mn =gmsh.model.mesh.field.add("Min")
        gmsh.model.mesh.field.setNumbers(mn, "FieldsList", fields)
        gmsh.model.mesh.field.setAsBackgroundMesh(mn)
        evened = 0
        if EVEN_FACES and kind != "triangles":
            fixed = {abs(c) for t in mapped for _d, c in gmsh.model.getBoundary([(2, t)], oriented=False)}
            floors = {t: plan_rules.FLOOR_WALLS * rows[k]["wall_mm"] * min(sz, 1.0) / mm for t, k in tag_of.items()}
            if sizing is not None and sizing.get("low_mm") is not None:
                # SHMESH-24: nor under the user's shortest edge
                low = float(sizing["low_mm"])
                floors = {t: max(v, low / mm) for t, v in floors.items()}
            evened = _even_faces(gmsh, tag_of, fixed, floors)
        _generate(gmsh, tag_of)
        node_tags, coords, _p = gmsh.model.mesh.getNodes()
        tags = np.asarray(node_tags, dtype=np.int64)
        index = np.full(int(tags.max()) + 1 if len(tags) else 1, -1, dtype=np.int64)
        index[tags] = np.arange(len(tags))
        xyz = np.asarray(coords, dtype=float).reshape(-1, 3) * mm
        quads, trias, q_face, t_face = [], [], [], []
        for t, k in sorted(tag_of.items()):
            types, _tags, conns = gmsh.model.mesh.getElements(2, t)
            for et, conn in zip(types, conns, strict=True):
                c = index[np.asarray(conn, dtype=np.int64)]
                if et == 3:
                    c = c.reshape(-1, 4)
                    quads.append(c)
                    q_face += [k] * len(c)
                elif et == 2:
                    c = c.reshape(-1, 3)
                    trias.append(c)
                    t_face += [k] * len(c)
                else:
                    raise ValueError(f"gmsh made an element of type {et} on a shell face")
        # SHMESH-14: the nodes inside a flat face (on no CAD edge) may move within its plane; SHMESH-14c: inside a
        # curved face too, laid back onto its surface
        movable = np.zeros(len(tags), dtype=bool)
        on_face = np.full(len(tags), -1, dtype=np.int64)
        for t in sorted(tag_of):
            plane = gmsh.model.getType(2, t) == "Plane"
            if plane or RELAX_CURVED:
                inner, _c, _p = gmsh.model.mesh.getNodes(2, t, includeBoundary=False)
                if len(inner):
                    at = index[np.asarray(inner, dtype=np.int64)]
                    movable[at] = True
                    if not plane:
                        on_face[at] = tag_of[t]
        # SHMESH-28: which nodes lie on a CAD edge (and on which) or at a CAD vertex - those the mend never takes off
        node_kind = np.zeros(len(tags), dtype=np.int8)
        node_edge = np.full(len(tags), -1, dtype=np.int64)
        vertex_edges: dict[int, set[int]] = {}
        # SHMESH-14d: the nodes on a straight CAD edge may slide along it (between its two ends)
        on_line = np.zeros(len(tags), dtype=bool)
        line_a = np.zeros((len(tags), 3))
        line_b = np.zeros((len(tags), 3))
        if MEND and lim.aims:
            for _d, c in gmsh.model.getEntities(1):
                inner, _c, _p = gmsh.model.mesh.getNodes(1, c, includeBoundary=False)
                at = index[np.asarray(inner, dtype=np.int64)]
                node_kind[at] = shell_quality.ON_EDGE
                node_edge[at] = int(c)
                points = [abs(int(p)) for _dd, p in gmsh.model.getBoundary([(1, c)], oriented=False)]
                for p in points:
                    ends, _c, _p = gmsh.model.mesh.getNodes(0, p)
                    for v in index[np.asarray(ends, dtype=np.int64)].tolist():
                        node_kind[v] = shell_quality.AT_VERTEX
                        vertex_edges.setdefault(int(v), set()).add(int(c))
                if SLIDE_EDGES and len(at) and len(points) == 2 and gmsh.model.getType(1, c) == "Line":
                    on_line[at] = True
                    line_a[at] = np.asarray(gmsh.model.getValue(0, points[0], []), dtype=float) * mm
                    line_b[at] = np.asarray(gmsh.model.getValue(0, points[1], []), dtype=float) * mm
        if log is not None:
            log.extend(gmsh.logger.get())
    finally:
        gmsh.finalize()
    Q = np.vstack(quads) if quads else np.zeros((0, 4), dtype=np.int64)
    T = np.vstack(trias) if trias else np.zeros((0, 3), dtype=np.int64)
    merged = 0
    if COLLAPSE_SHORT:
        # SHMESH-12: an edge under the floor, far shorter than its elements' others, is a CAD sliver: one node
        limit = np.array([plan_rules.FLOOR_WALLS * r["wall_mm"] * min(sz, 1.0) / mm for r in rows])
        Q, T, q_face, t_face, merged = shell_mesh.collapse_short(xyz / mm, Q, T, q_face, t_face, limit)
    relaxed = 0
    project = _projector(facts, on_face, mm) if RELAX_CURVED and (on_face >= 0).any() else None
    if on_line.any():
        # SHMESH-14d (under the user's limits): the nodes on straight CAD edges relax too, each kept on its edge
        project = _slider(project, on_line, line_a, line_b)
        movable = movable | on_line
    if RELAX_ROUNDS:
        # the neighbours' middle first; then also where each quad round a node would be a parallelogram (SHMESH-14b),
        # for what the middle left past a limit (tried at once, the parallelogram moves mended a quad and left a
        # sliver triangle beside it that the middle had mended)
        xyz, relaxed = shell_quality.relax_past(xyz, Q, T, movable, rounds=RELAX_ROUNDS, parallelogram=False,
                                                project=project, limits=lim)
        if shell_quality.RELAX_PARALLELOGRAM:
            xyz, again = shell_quality.relax_past(xyz, Q, T, movable, rounds=RELAX_ROUNDS, parallelogram=True,
                                                  project=project, limits=lim)
            relaxed += again
    split = 0
    if SPLIT_PAST:
        # SHMESH-10: a quad past Nastran's own limits becomes two triangles
        keep, cut, came = shell_quality.split_past_quads(xyz, Q, lim)
        if len(cut):
            qf = np.array(q_face, dtype=np.int64)
            T = np.vstack([T, cut])
            t_face = t_face + qf[came].tolist()
            Q, q_face = Q[keep], qf[keep].tolist()
            split = len(came) // 2
        if COLLAPSE_SHORT and split:
            # a quad folded nearly flat (two opposite corners a sliver apart) cuts into two slivers along that short
            # diagonal: it closes up, its edges zipped pairwise (SHMESH-12 on the cut)
            Q, T, q_face, t_face, again = shell_mesh.collapse_short(xyz / mm, Q, T, q_face, t_face, limit)
            merged += again
            if again:
                # the merge can leave a neighbouring quad past a limit (a corner pulled onto the merged node):
                # SHMESH-10 once more on what it left
                keep, cut, came = shell_quality.split_past_quads(xyz, Q, lim)
                if len(cut):
                    qf = np.array(q_face, dtype=np.int64)
                    T = np.vstack([T, cut])
                    t_face = t_face + qf[came].tolist()
                    Q, q_face = Q[keep], qf[keep].tolist()
                    split += len(came) // 2
    aimed = 0
    if lim.aims and kind != "quads" and len(Q):
        # SHMESH-25: a quad past one of the USER's limits is cut only when both triangles are inside every limit,
        # and only as far as the share the user allows needs (never when they asked for quads)
        keep, cut, came = shell_quality.split_aimed_quads(xyz, Q, len(Q) + len(T), lim)
        if len(cut):
            qf = np.array(q_face, dtype=np.int64)
            T = np.vstack([T, cut])
            t_face = t_face + qf[came].tolist()
            Q, q_face = Q[keep], qf[keep].tolist()
            aimed = len(came) // 2
    flipped = 0
    if FLIP_PAST and len(T):
        # SHMESH-13: a triangle past a limit swaps its longest edge with its neighbour's when both come out better
        T, flipped = shell_quality.flip_past_trias(xyz, T, t_face, limits=lim)
    if RELAX_ROUNDS and (split or aimed) and len(T):
        # SHMESH-14 once more, after the cut, for the triangles it left past a limit; no quad may come out past one
        # (a quad past a limit outweighs every triangle round a node)
        xyz, again = shell_quality.relax_past(xyz, Q, T, movable, rounds=RELAX_ROUNDS, weights=(64, 1),
                                              project=project, limits=lim)
        relaxed += again
    paired = 0
    if PAIR_TRIAS and len(T) > 1 and kind != "triangles":
        # SHMESH-17: two triangles of a face that make a quad inside Nastran's limits become that quad
        new_q, kept, came = shell_quality.pair_trias(xyz, Q, T, t_face, lim)
        if len(new_q):
            tf = np.array(t_face, dtype=np.int64)
            Q = np.vstack([Q, new_q])
            q_face = q_face + tf[came].tolist()
            T, t_face = T[kept], tf[kept].tolist()
            paired = len(new_q)
    walked = 0
    if WALK_DEPTH and len(T) > 1 and kind != "triangles":
        # SHMESH-18: a triangle walked through its face's quads to the nearest other triangle, the two made one quad
        Q, T, q_face, t_face, walked = shell_quality.walk_trias(xyz, Q, T, q_face, t_face, depth=WALK_DEPTH,
                                                                limits=lim)
    capped = 0
    if ABSORB_CAPS and len(T):
        # SHMESH-22: a cap left past a limit (all its corners on CAD edges) taken into the element across its longest
        # edge and cut again inside the limits, or dropped from a free edge
        face_wall = np.array([float(r["wall_mm"]) for r in rows])
        Q, T, q_face, t_face, capped = shell_quality.absorb_caps(xyz, Q, T, q_face, t_face, face_wall, limits=lim)
    mended = {"merged": 0, "recut": 0, "moved": 0}
    if MEND and lim.aims:
        # SHMESH-28: what is still past a hard limit or outside one of the user's limits is mended by merging a
        # node away or cutting two neighbours' outline again; the nodes round a change relax once more, and what
        # that opens is mended again
        # SHMESH-29: "ignore edges under X mm" - only when the user asks: the ends of a CAD edge shorter than X may be
        # merged too, and every element edge under X counts as outside
        ignore = ((requirements or {}).get("sizes") or {}).get("ignore_edges_mm")
        ignore = float(ignore) if ignore else None
        for _ in range(MEND_CYCLES):
            xyz, Q, T, q_face, t_face, got = shell_quality.mend(
                xyz, Q, T, q_face, t_face, node_kind, node_edge, vertex_edges, limits=lim,
                quads_too=kind != "triangles", triangles_too=kind != "quads",
                edges=(ignore if ignore else ((sizing or {}).get("low_mm") if MEND_SHORT else None),
                       (sizing or {}).get("high_mm") if MEND_LONG else None), suppress=ignore,
                movable=movable if MEND_MOVES else None, project=project)
            if not (got["merged"] or got["recut"] or got["moved"]):
                break
            mended = {k: mended[k] + got[k] for k in mended}
            if RELAX_ROUNDS:
                xyz, again = shell_quality.relax_past(xyz, Q, T, movable, rounds=RELAX_ROUNDS, weights=(64, 1),
                                                      project=project, limits=lim)
                relaxed += again
    # keep only the nodes the elements use, renumbered in order
    used = np.unique(np.concatenate([Q.ravel(), T.ravel()]))
    remap = np.full(len(xyz), -1, dtype=np.int64)
    remap[used] = np.arange(len(used))
    Q, T, xyz = remap[Q], remap[T], xyz[used]
    face_of = np.array(q_face + t_face, dtype=np.int64)
    mids = {m["id"]: m for m in record["mids"]}
    pairs = {p["id"]: p for p in record["pairs"]}
    mid_of = [owners[k] for k in face_of.tolist()]
    wall = np.array([float(pairs[mids[m]["pair"]]["thickness_mm"]) for m in mid_of])
    part = [mids[m]["body"] for m in mid_of]
    mesh = ShellMesh(xyz=xyz, quads=Q, trias=T, wall=wall, mid=mid_of, part=part)
    # groups: one per wall pair (its CAD faces), one per part
    for m in sorted(set(mid_of), key=lambda s: int(s.split("_")[1])):
        name = "+".join(mids[m]["faces"])
        mesh.groups.setdefault(name, np.zeros(0, dtype=np.int64))
        mesh.groups[name] = np.concatenate([mesh.groups[name], np.flatnonzero(np.array(mid_of) == m)])
    for p in sorted(set(part)):
        mesh.groups[p] = np.flatnonzero(np.array(part) == p)
    reference: dict[str, np.ndarray] = {}
    for k, f in enumerate(facts):
        n = _reference_normal(f["normal_face"])
        if n is not None:
            reference.setdefault(owners[k], n)
    turned = orient(mesh, reference)
    varied = node_walls(mesh, mids)
    offset = element_offsets(mesh, mids)
    census = edge_census(mesh)
    quality = shell_quality.measure(mesh)
    quality["edges"] = {k: v for k, v in census.items() if k != "free_edges"}
    quality["turned"] = turned
    quality["varied_wall_elements"] = varied
    quality["offset_elements"] = offset
    quality["split"] = split
    quality["relaxed_nodes"] = relaxed
    quality["merged_nodes"] = merged
    quality["flipped"] = flipped
    quality["paired"] = paired
    quality["walked"] = walked
    quality["caps_absorbed"] = capped
    quality["evened_curves"] = evened
    quality["sliver_faces"] = slivers
    if slivers:
        mesh.notes.append(f"{slivers} sewn face(s) under a fifth of the floor across (CAD slivers) carry no "
                          "elements (SHMESH-12).")
    if merged:
        mesh.notes.append(f"{merged} node(s) at the ends of sliver edges (under the floor, a fifth of their elements' longest) "
                          "were merged (SHMESH-12).")
    if capped:
        mesh.notes.append(f"{capped} flat triangle(s) with every corner on a CAD edge were taken into the element "
                          "beside them, or dropped from a free edge (SHMESH-22).")
    if split:
        mesh.notes.append(f"{split} quad(s) past Nastran's own limits were cut into two triangles each (SHMESH-10).")
    floored = [r["face"] for r in rows if r["floored"]]
    if floored:
        mesh.notes.append(f"{len(floored)} face(s) asked for elements finer than half their wall; they are meshed at "
                          "half the wall (SHMESH-2).")
    out = {"faces": rows, "quality": quality, "per_turn": PER_TURN, "growth": growth}
    if counted is not None:
        out["target"] = counted
    if requirements:
        # SHMESH-26: every requirement of the user's, asked against delivered, measured on this mesh
        quality["aimed_split"] = aimed
        quality["mended"] = mended
        if aimed:
            mesh.notes.append(f"{aimed} quad(s) past a limit you asked for were cut into two triangles each, both "
                              "inside it (SHMESH-25).")
        if sizing is not None:
            out["sizing"] = sizing
            if narrow is not None and (narrow["widths"] or short_edges["edges"]):
                smallest = float(f"{min([*narrow['widths'].values(), *short_edges['lengths'].values()]):.4g}")
                sizing["features"] = {"rule": "SHMESH-27", "faces": len(narrow["widths"]),
                                      "edges": short_edges["edges"], "smallest_mm": smallest}
                mesh.notes.append(
                    f"Your CAD has {short_edges['edges']} edge(s) and {len(narrow['widths'])} face(s) smaller than "
                    f"the element size you asked for (the smallest is {smallest:g} mm). The mesh is finer around "
                    "them, so the elements there keep a good shape (SHMESH-27).")
            flanks = sorted((narrow or {}).get("flank_faces") or [])
            if flanks:
                sizing["flanks"] = {"rule": "SHMESH-35", "faces": len(flanks), "across": plan_rules.FLANK_ACROSS}
                mesh.notes.append(f"Kept finer at {len(flanks)} narrow wall(s) of beads, darts or steps: "
                                  f"{plan_rules.FLANK_ACROSS} elements across each, so they keep their shape "
                                  "(SHMESH-35).")
            chord = _chord(mesh, facts, face_of, mm) if CHORD_REPORT else None
            if chord is not None:
                # SHMESH-34: how far the elements stand off the CAD on curved faces, told against the size
                g = sizing.get("global_mm") or float(np.median([r["size_mm"] for r in rows]))
                sizing["chord"] = {"rule": "SHMESH-34", **chord, "share": float(f"{chord['max_mm'] / g:.3g}")}
                mesh.notes.append(f"On curved faces element centres lie within {chord['max_mm']:.3g} mm of your "
                                  f"CAD ({100 * chord['max_mm'] / g:.2g}% of the element size) (SHMESH-34).")
        asked = SR.report(mesh, requirements, sizing)
        if asked is not None:
            out["requirements"] = asked
        # SHMESH-31: where elements past the solver's own limits are (taken off by _mesh_shell)
        past = shell_quality.nastran_past(mesh) | (_min_jac(mesh) <= 0)
        out["_past_spots"] = _spots(mesh, face_of, past)
        out["_past_n"] = int(past.sum())
    return mesh, out


def _spots(mesh: ShellMesh, face_of: np.ndarray, past: np.ndarray) -> list[tuple]:
    """SHMESH-31: boxes (sewn face, low corner, high corner; mm) round the elements `past`, each padded by its
    element's longest edge; boxes of one face that overlap are joined, and past SPOTS_MAX boxes each face's are
    joined into one."""
    boxes: list[list] = []
    for e in np.flatnonzero(past).tolist():
        p = mesh.xyz[mesh.element_nodes(int(e))]
        pad = float(np.linalg.norm(p - np.roll(p, 1, axis=0), axis=1).max())
        boxes.append([int(face_of[e]), p.min(axis=0) - pad, p.max(axis=0) + pad])

    def join(bs: list[list], always: bool) -> list[list]:
        out: list[list] = []
        for b in sorted(bs, key=lambda x: (x[0], *x[1].tolist())):
            for o in out:
                if o[0] == b[0] and (always or (np.all(o[1] <= b[2]) and np.all(b[1] <= o[2]))):
                    o[1], o[2] = np.minimum(o[1], b[1]), np.maximum(o[2], b[2])
                    break
            else:
                out.append([b[0], b[1].copy(), b[2].copy()])
        return out

    boxes = join(boxes, False)
    if len(boxes) > SPOTS_MAX:
        boxes = join(boxes, True)
    return [(k, tuple(float(f"{v:.6g}") for v in lo), tuple(float(f"{v:.6g}") for v in hi)) for k, lo, hi in boxes]


def _min_jac(mesh: ShellMesh) -> np.ndarray:
    """Each element's smallest corner Jacobian (quads, then triangles)."""
    q = shell_quality.quad_measures(mesh.xyz, mesh.quads)["min_jacobian"] if len(mesh.quads) else np.zeros(0)
    t = shell_quality.tria_measures(mesh.xyz, mesh.trias)["min_jacobian"] if len(mesh.trias) else np.zeros(0)
    return np.concatenate([q, t])


def _narrow_faces(rows: list[dict], facts: list[dict], mm: float, planned: float,
                  interior: set[int] | None = None) -> dict | None:
    """SHMESH-27, faces: under the user's sizes every face narrower than 1 / FEATURE_FACE_RATIO of its size (its
    strip width: twice its area over its perimeter) is sized to FEATURE_FACE_RATIO times its own width, never under
    the floor, in place - a 0.3 mm step strip at a 3 mm size was one row of elements ten times as long as wide. With
    FEATURE_FACES off, only a face narrower than the floor itself (half its wall: a sliver strip) is: it takes the
    floor, as our own rules size it, so the sliver merge (SHMESH-12) closes it as it does with no notes (fr014's
    0.28 mm step strips at 3.2 mm: 137 triangles past Nastran's T3_IAMAX; none with no notes). Returns {"was": the
    rows changed as they were, "widths": their widths, "planned"}; None (rows untouched) when the finer plan would
    pass SHMESH-20's most elements.
    SHMESH-35: a FLANK - a face narrower than its size with no free edge (every side meets another face: a bead's or
    a dart's wall, a step, a rib) that is not itself a tight bend - keeps plan_rules.FLANK_ACROSS elements across it
    (never under the floor), so a bead is not washed out under a coarse size (`interior`: the faces with no free
    edge; None: no flank rule). Returns also "flanks": how many faces it kept finer."""
    new: dict[int, float] = {}
    widths: dict[int, float] = {}
    flank_faces: set[int] = set()
    for k, (f, r) in enumerate(zip(facts, rows, strict=True)):
        width = min(f["width"], f["box_width"]) if f["box_width"] > 0 else f["width"]
        if interior is not None and k in interior and not (
                f["bend"] is not None and 0 < f["bend"] < plan_rules.PANEL_WALLS * r["wall_mm"]):
            h = plan_rules.flank_size(width, size=r["size_mm"], wall=r["wall_mm"])
            if h is not None:
                new[k], widths[k] = h, width
                flank_faces.add(k)
                continue
        if not FEATURE_FACES and width >= plan_rules.FLOOR_WALLS * r["wall_mm"]:
            continue
        h = plan_rules.feature_size(width, size=r["size_mm"], wall=r["wall_mm"], ratio=plan_rules.FEATURE_FACE_RATIO)
        if h is not None:
            new[k] = h
            widths[k] = width
    extra = sum(facts[k]["area"] * mm * mm * (1.0 / h ** 2 - 1.0 / rows[k]["size_mm"] ** 2) for k, h in new.items())
    if planned + extra > PLANNED_MAX:
        return None
    was = {k: dict(rows[k]) for k in new}
    for k, h in new.items():
        rows[k]["size_mm"] = h
        if rows[k].get("curve_mm", 0.0) >= h:
            rows[k].pop("curve_mm", None)
        rows[k]["rule"] = f"{rows[k]['rule']}+SHMESH-27"
    return {"was": was, "widths": widths, "planned": planned + extra, "flank_faces": flank_faces}


def _chord(mesh: ShellMesh, facts: list[dict], face_of: np.ndarray, mm: float) -> dict | None:
    """SHMESH-34: on the curved faces (those with a radius), the largest distance (mm) of an element's centre from the
    face's own surface, over at most CHORD_SAMPLES elements a face (every n-th, in order); None with no curved face."""
    from OCP.BRep import BRep_Tool
    from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
    from OCP.gp import gp_Pnt

    worst, faces = 0.0, 0
    order = np.argsort(face_of, kind="stable")
    bounds = np.searchsorted(face_of[order], np.arange(len(facts) + 1))
    for k, f in enumerate(facts):
        if f["bend"] is None:
            continue
        els = order[bounds[k]:bounds[k + 1]]
        if not len(els):
            continue
        faces += 1
        surf = BRep_Tool.Surface_s(f["normal_face"])
        for e in els[::max(1, len(els) // CHORD_SAMPLES)].tolist():
            c = mesh.xyz[mesh.element_nodes(int(e))].mean(axis=0) / mm
            pr = GeomAPI_ProjectPointOnSurf(gp_Pnt(float(c[0]), float(c[1]), float(c[2])), surf)
            if pr.NbPoints() > 0:
                worst = max(worst, float(pr.LowerDistance()) * mm)
    return {"max_mm": float(f"{worst:.4g}"), "faces": faces} if faces else None


def _interior_faces(brep: Path) -> set[int]:
    """SHMESH-35: the sewn faces (0-based) every edge of which another face shares too (no free edge)."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS_Shape
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    from meshpipeline.cad import midsurface as ms

    shape = TopoDS_Shape()
    if not BRepTools.Read_s(shape, str(brep), BRep_Builder()):
        raise ValueError("the mid-surface could not be read")
    fmap = ms._map(shape, TopAbs_FACE)
    emap = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, emap)
    free: set[int] = set()
    for i in range(1, emap.Extent() + 1):
        ks = {fmap.FindIndex(x) - 1 for x in emap.FindFromIndex(i)} - {-1}
        if len(ks) == 1:
            free |= ks
    return set(range(fmap.Extent())) - free


def _spare_strip_corners(gmsh, tag_of: dict[int, int], rows: list[dict], mapped: dict, narrow: dict,
                         short_edges: dict) -> None:
    """SHMESH-27: a mapped grid cannot grade, so a smaller piece of the CAD at a mapped strip's corner is left at the
    size it had: a short edge ending there is not graded to, a narrow face (not itself mapped) with a corner there
    takes its size back. Graded, a tube corner's 10 mm mapped elements fanned out of a 0.5 mm face at its end
    (sh0110: 30 triangles under 20 degrees became 168); left free, the strips lost their grids (899)."""
    def points(c: int) -> set[int]:
        return {abs(int(p)) for _d, p in gmsh.model.getBoundary([(1, c)], oriented=False)}

    def curves(t: int) -> list[int]:
        return [abs(int(c)) for _d, c in gmsh.model.getBoundary([(2, t)], oriented=False)]

    corners: set[int] = set()
    for t in mapped:
        for c in curves(t):
            corners |= points(c)
    for h in list(short_edges["by_size"]):
        keep = [c for c in short_edges["by_size"][h] if not (points(c) & corners)]
        for c in short_edges["by_size"][h]:
            if c not in keep:
                short_edges["lengths"].pop(c, None)
        short_edges["edges"] = len(short_edges["lengths"])
        if keep:
            short_edges["by_size"][h] = keep
        else:
            del short_edges["by_size"][h]
    face_tag = {k: t for t, k in tag_of.items()}
    for k in sorted(narrow["was"]):
        t = face_tag.get(k)
        if t is None or t in mapped:
            continue
        if any(points(c) & corners for c in curves(t)):
            rows[k].clear()
            rows[k].update(narrow["was"].pop(k))
            narrow["widths"].pop(k)
            narrow["flank_faces"].discard(k)


def _short_edges(gmsh, tag_of: dict[int, int], rows: list[dict], mm: float) -> dict:
    """SHMESH-27, edges: every edge of the CAD shorter than 1 / FEATURE_RATIO of the size of the faces it bounds
    takes FEATURE_RATIO times its own length as the size beside it (grown from there by the grading fields). The
    edge itself still gets one segment. Returns {"by_size": {size mm: [curves]}, "edges", "lengths": {curve: mm},
    "fine_at": {CAD vertex: the finest size (mm) a feature gives beside it - a short edge ending there, a narrow
    face (one SHMESH-27 sized) it is a corner of}}."""
    by_size: dict[float, list[int]] = {}
    fine_at: dict[int, float] = {}
    lengths: dict[int, float] = {}

    def ends(c: int) -> list[int]:
        return [abs(int(p)) for _d, p in gmsh.model.getBoundary([(1, c)], oriented=False)]

    for _d, c in gmsh.model.getEntities(1):
        faces = [int(t) for t in gmsh.model.getAdjacencies(1, c)[0] if int(t) in tag_of]
        if not faces or not FEATURE_EDGES:
            continue
        for t in faces:
            if "SHMESH-27" in rows[tag_of[t]]["rule"]:
                for p in ends(int(c)):
                    fine_at[p] = min(fine_at.get(p, math.inf), float(rows[tag_of[t]]["size_mm"]))
        size = min(rows[tag_of[t]]["size_mm"] for t in faces)
        wall = min(rows[tag_of[t]]["wall_mm"] for t in faces)
        length = float(gmsh.model.occ.getMass(1, c)) * mm
        h = plan_rules.feature_size(length, size=size, wall=wall, ratio=plan_rules.FEATURE_RATIO)
        if h is None:
            continue
        by_size.setdefault(float(f"{h:.6g}"), []).append(int(c))
        for p in ends(int(c)):
            fine_at[p] = min(fine_at.get(p, math.inf), h)
        lengths[int(c)] = length
    return {"by_size": by_size, "edges": len(lengths), "fine_at": fine_at, "lengths": lengths}


def _count_plan(rows: list[dict], planned: float, target: dict | None, correction: float) -> tuple[dict | None, float]:
    """SHMESH-23: the plan's rows scaled in place to the user's element count: (what was done, the factor on every
    size); (None, 1.0) with no count asked. `planned` is the rules' own estimate, `correction` the built-over-planned
    measured on a first mesh (1.0 before one)."""
    if not target or int(target.get("elements") or 0) <= 0:
        return None, 1.0
    corr = float(correction) if correction and correction > 0 else 1.0
    rules = planned * corr
    aim = plan_rules.count_aim(rules, target)
    out = {"asked": int(target["elements"]), "mode": str(target.get("mode") or "about"),
           "rules_elements": int(round(rules)), "correction": round(corr, 4)}
    if aim is None:
        out.update(kept_rules=True, scale=1.0, planned=int(round(planned)))
        return out, 1.0
    sz = plan_rules.count_scale(rules, aim)
    held = sz in plan_rules.SCALE_RANGE
    # past what a shell mesh is made of in the time allowed (SHMESH-20), the count is held there, and said
    if planned / sz ** 2 > PLANNED_MAX:
        sz, held = math.sqrt(planned / (0.95 * PLANNED_MAX)), True
    under = False
    for r in rows:
        r["size_mm"] = float(f"{r['size_mm'] * sz:.6g}")
        if "curve_mm" in r:
            r["curve_mm"] = float(f"{r['curve_mm'] * sz:.6g}")
        r["floored"] = False
        finest = min(r["size_mm"], r.get("curve_mm", r["size_mm"]))
        under = under or finest < plan_rules.FLOOR_WALLS * r["wall_mm"] * (1 - 1e-6)
        r["rule"] = f"{r['rule']}+SHMESH-23"
    out.update(scale=round(sz, 4), planned=int(round(planned / sz ** 2)), under_floor=under, held=held)
    return out, sz


def _projector(facts: list[dict], on_face: np.ndarray, mm: float):
    """SHMESH-14c: project(nodes, points) - each point laid onto its node's own curved face's surface (the closest
    point; mm in and out). Nodes of flat faces (on_face -1) are left as given (the middle of coplanar points stays
    in their plane)."""
    from OCP.BRep import BRep_Tool
    from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
    from OCP.gp import gp_Pnt

    surfaces: dict[int, object] = {}

    def project(nodes: np.ndarray, pts: np.ndarray) -> np.ndarray:
        out = pts.copy()
        for i, v in enumerate(nodes.tolist()):
            k = int(on_face[v])
            if k < 0:
                continue
            if k not in surfaces:
                surfaces[k] = BRep_Tool.Surface_s(facts[k]["normal_face"])
            p = pts[i] / mm
            pr = GeomAPI_ProjectPointOnSurf(gp_Pnt(float(p[0]), float(p[1]), float(p[2])), surfaces[k])
            if pr.NbPoints() > 0:
                q = pr.NearestPoint()
                out[i] = np.array([q.X(), q.Y(), q.Z()]) * mm
        return out

    return project


def _slider(project, on_line: np.ndarray, line_a: np.ndarray, line_b: np.ndarray):
    """SHMESH-14d: project(nodes, points) - a node on a straight CAD edge is laid back onto that edge (the closest
    point of the line through the edge's two ends, kept between them: SLIDE_ENDS of its length from either end);
    every other node goes through `project` (the curved faces' own, or none)."""
    def both(nodes: np.ndarray, pts: np.ndarray) -> np.ndarray:
        out = project(nodes, pts) if project is not None else pts.copy()
        sel = on_line[nodes]
        if sel.any():
            a, b = line_a[nodes[sel]], line_b[nodes[sel]]
            d = b - a
            t = np.einsum("mi,mi->m", pts[sel] - a, d) / np.maximum(np.einsum("mi,mi->m", d, d), 1e-300)
            out[sel] = a + np.clip(t, SLIDE_ENDS, 1.0 - SLIDE_ENDS)[:, None] * d
        return out

    return both


#: SHELL-9b: a node's wall is read from the WALL_NEAREST wall readings of its mid-face nearest it (a plane through
#: them, the nearer weighing more, so a wall that varies evenly - a taper, a draft - is read exactly), kept within
#: their thinnest and thickest. The 8 nearest were all one row of a narrow strip's readings: no slope along it
WALL_NEAREST = 24
#: a mid-face's wall varies when its thickest reading is more than WALL_VARIES thicker than its thinnest
WALL_VARIES = 0.01


def _wall_at(points: np.ndarray, samples: np.ndarray) -> np.ndarray:
    """The wall at each point (mm): a least-squares plane through the WALL_NEAREST readings nearest it
    (x, y, z, wall rows of `samples`), clipped to the readings' range."""
    xyz, w = samples[:, :3], samples[:, 3]
    k = min(WALL_NEAREST, len(samples))
    out = np.empty(len(points))
    for i, p in enumerate(points):
        d = np.linalg.norm(xyz - p, axis=1)
        near = np.argsort(d, kind="stable")[:k]
        rel = xyz[near] - p
        # the readings lie on a surface: fit over its two widest directions there (a 3D fit is near-singular)
        _u, sv, vt = np.linalg.svd(rel - rel.mean(axis=0), full_matrices=False)
        A = np.column_stack([np.ones(k), rel @ vt[:2].T])
        # nearer readings weigh more (a wall that varies unevenly is read locally)
        wt = 1.0 / (d[near] + float(np.median(d[near])) + 1e-300)
        coef, *_rest = np.linalg.lstsq(A * wt[:, None], w[near] * wt, rcond=1e-6)
        out[i] = coef[0]
    return np.clip(out, float(w.min()), float(w.max()))


def node_walls(mesh: ShellMesh, mids: dict[str, dict]) -> int:
    """SHELL-9b: every node of a mid-face whose wall varies (a tapered or drafted wall, fitted) gets its own wall,
    read from the record's wall readings; its elements are written per corner (CQUAD4 T1..T4, *NODAL
    THICKNESS). A varying mid-face without readings is never meshed at one wall: it stops here. Returns how many
    elements carry corner walls."""
    mid_of = np.array(mesh.mid)
    varies = np.zeros(mesh.n_elements, dtype=bool)
    total = np.zeros(len(mesh.xyz))
    count = np.zeros(len(mesh.xyz))
    for m in sorted(set(mesh.mid), key=lambda x: int(x.split("_")[1])):
        row = mids[m]
        lo, hi = row.get("thickness_range_mm", [row.get("thickness_mm"), row.get("thickness_mm")])
        if not (lo and hi and hi > lo * (1.0 + WALL_VARIES)):
            continue
        samples = np.asarray(row.get("wall_samples_mm") or [], dtype=float).reshape(-1, 4)
        if len(samples) < 3:
            raise ValueError(f"{m}'s wall varies ({lo:g} to {hi:g} mm) but the record holds no wall readings: it "
                             "cannot be meshed at one wall (SHELL-9b)")
        els = np.flatnonzero(mid_of == m)
        nodes = np.unique(np.concatenate([mesh.element_nodes(int(e)) for e in els]))
        total[nodes] += _wall_at(mesh.xyz[nodes], samples)
        count[nodes] += 1.0
        varies[els] = True
    if not varies.any():
        return 0
    mesh.node_wall = np.where(count > 0, total / np.maximum(count, 1.0), 0.0)
    mesh.varies = varies
    used = np.unique(np.concatenate([mesh.element_nodes(int(e)) for e in np.flatnonzero(varies)]))
    if np.any(mesh.node_wall[used] <= 0):
        raise ValueError("a node of a varying wall came out with no wall (SHELL-9b)")
    return int(varies.sum())


def element_offsets(mesh: ShellMesh, mids: dict[str, dict]) -> int:
    """SHELL-7: an element of a mid-face snapped onto a thicker neighbour's mid-plane (a thickness step, a lap)
    carries the distance from its nodes' plane to its own true mid-plane, along its own normal (Nastran ZOFFS,
    Abaqus / CalculiX OFFSET, Ansys SECOFFSET). Returns how many elements are offset."""
    from meshpipeline.engines.shell_mesh import _normal

    z = np.zeros(mesh.n_elements)
    for e, m in enumerate(mesh.mid):
        row = mids[m]
        off = float(row.get("offset_mm") or 0.0)
        if not off:
            continue
        n_o = np.asarray(row.get("offset_normal") or [0.0, 0.0, 0.0], dtype=float)
        if not np.linalg.norm(n_o):
            raise ValueError(f"{m} is offset {off:g} mm but the record gives no direction (SHELL-7)")
        n_e = _normal(mesh.xyz, mesh.element_nodes(e))
        z[e] = off * float(np.dot(n_o, n_e / max(float(np.linalg.norm(n_e)), 1e-300)))
    if not np.any(z):
        return 0
    mesh.zoffs = z
    return int(np.count_nonzero(z))


def mesh_run(out_dir: Path | str, *, mm_per_unit: float = 1.0, target: dict | None = None,
             requirements: dict | None = None) -> tuple[ShellMesh, dict]:
    """Mesh the mid-surface cad/midsurface wrote into `out_dir`, and write shell_plan.json there. `target`: the
    user's element count (SHMESH-23), met on the built mesh - aimed again once when the first mesh missed it.
    `requirements`: the mesh requirements read from the user's notes (SHMESH-24..26); a global size among them
    decides the sizes, so a count asked beside it is set aside (and said: applied["count_set_aside"])."""
    from meshpipeline.cad import midsurface as ms

    out = Path(out_dir)
    record = json.loads((out / ms.RECORD_FILE).read_text())
    set_aside = None
    if requirements and ((requirements.get("sizes") or {}).get("global_mm") is not None) and target \
            and int(target.get("elements") or 0) > 0:
        set_aside, target = int(target["elements"]), None
    mesh, applied = mesh_shell(out / ms.SURFACE_FILE, record, mm_per_unit=mm_per_unit, target=target,
                               requirements=requirements)
    if set_aside:
        applied["count_set_aside"] = set_aside
    t = applied.get("target")
    if t and not t.get("kept_rules") and not plan_rules.count_met(mesh.n_elements, target):
        # SHMESH-23: the built count missed the user's: aimed again once on the built-over-planned it measured, and
        # the closer of the two kept
        corr = float(t.get("correction") or 1.0) * mesh.n_elements / max(float(t.get("planned") or 1), 1.0)
        try:
            again, applied2 = mesh_shell(out / ms.SURFACE_FILE, record, mm_per_unit=mm_per_unit, target=target,
                                         correction=corr, requirements=requirements)
        except ValueError as exc:
            logger.info("shell mesh: the count was not aimed again (%s)", exc)
        else:
            aim = float(t["asked"])

            def gap(n: int) -> float:
                return abs(math.log(max(n, 1) / aim))

            if gap(again.n_elements) < gap(mesh.n_elements):
                mesh, applied = again, applied2
            applied["target"]["reaimed"] = True
    if applied.get("target"):
        tt = applied["target"]
        tt["built"] = int(mesh.n_elements)
        tt["held"] = bool(tt.get("held")) or (not tt.get("kept_rules") and not plan_rules.count_met(mesh.n_elements, target))
    (out / "shell_plan.json").write_text(json.dumps(applied, indent=1, sort_keys=True, default=float))
    return mesh, applied


def element_turn_deg(radius_mm: float, size_mm: float) -> float:
    """How far one element turns on a curve (degrees): the SHMESH-4 check."""
    return math.degrees(size_mm / radius_mm) if radius_mm > 0 else 0.0


__all__ = ["ALGORITHM", "PER_TURN", "mesh_run", "mesh_shell"]
