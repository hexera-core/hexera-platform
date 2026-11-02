# Responsibility: SPLIT A MIXED BODY (thin walls and thick regions, cad/midsurface's "mixed" verdict) into the part
# meshed as shell and the part kept solid: the thick regions found as what the shell walls do not hold, each grown a
# measured distance into the walls round it (MIXED-2, placeholder), the body cut there, the walls' mid-faces trimmed
# to the same cut - so every shell edge on the solid lies on a cut face, the wall's own cross-section.
# Boundaries: geometry only, deterministic, OpenCASCADE (offsets, Booleans, one thread). It meshes nothing and
# writes no file; it reads the extraction's record and sewn mid-surface and never changes how they are made.
# Collaborates with: cad/midsurface.py (the record, the sewn surface, its helpers), engines/mixed_mesh.py (meshes the
# two parts and joins them), engines/mixed_links.py (the join), docs/design/shell/README.md section 2.5.
"""MIXED-1..3: WHERE A MIXED BODY IS SHELL AND WHERE IT IS SOLID.

MIXED-1, THE THICK CORE: each shell wall's slab (its sewn mid-face thickened to its wall, half each side, a hair
over) is taken from the body. What is left and holds a face the extraction called thick or solid (a boss, a pad, a
block, a flange, a casting's lump) is a thick core. What is left and holds none is the metal of a junction between
shell walls (a fillet, a corner a shell model leaves out) and is not kept - as in a shell-only part.

MIXED-2, THE CUT: each core is grown CUT_WALLS of the thickest wall that runs into it: each of its faces inside the
body (where a wall's slab was taken off it - the wall's own cross-section) is swept that far along its outward
normal, and the body's metal in the core and its sweeps is the solid region. Inside a wall the sweep's far side is
the wall's cross-section again, square to it (a straight end a plane, a boss's circle a cylinder of radius r + 2t,
a tube's end a plane across it); at a square pad's corner the cut stays square. The cut is made where the wall's
stress is a plate's again. A wall's stress is
three-dimensional only about one wall from a junction (Saint-Venant; Abaqus' shell-to-solid coupling spreads
each shell node over half a wall either side); two walls keeps the join out of it. Each shell mid-face is trimmed by
the same grown core, so its edge lies on the cut face, along the wall's middle.

MIXED-3, ACCOUNTED FOR: the solid region's volume plus each trimmed mid-face's area times its wall holds the body's
volume within the shell check's band (VOLUME_BAND: a shell model leaves its junctions' fillets out); a split that
does not is not used, and the body is meshed solid, with the reason.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from meshpipeline.cad import midsurface as ms

logger = logging.getLogger(__name__)

#: MIXED-2: how far each thick core is grown into the walls that run into it, in walls (the thickest of them)
CUT_WALLS = 2.0
#: MIXED-1: a wall's slab reaches this share of a wall past each of its faces (the faces are the slab's to take)
SLAB_SLACK = 0.02
#: MIXED-1: a left-over piece holding no thick face but more than this share of the body's volume is a wall the
#: slabs missed - the split is not used
LEFT_OVER_MAX = 0.02
#: MIXED-3: the split must hold the body's metal within this band (the shell check's own)
VOLUME_BAND = ms.VOLUME_BAND


@dataclass
class Split:
    """The split of one body: the solid region, the trimmed mid-faces (each with the index of the sewn face it came
    from), the cut faces of the solid region (each with the wall of the shell that ends on it), the words."""
    solid: object = None
    shell_faces: list = field(default_factory=list)
    shell_from: list[int] = field(default_factory=list)
    cut_faces: list = field(default_factory=list)
    cut_walls: list[float] = field(default_factory=list)
    cores: int = 0
    core_volumes: list[float] = field(default_factory=list)
    solid_volume: float = 0.0
    shell_volume: float = 0.0
    body_volume: float = 0.0
    dropped_volume: float = 0.0
    cut_distance: list[float] = field(default_factory=list)
    ok: bool = False
    why: str = ""
    notes: list[str] = field(default_factory=list)


def _solids(shape) -> list:
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopoDS import TopoDS

    m = ms._map(shape, TopAbs_SOLID)
    return [TopoDS.Solid_s(m.FindKey(i)) for i in range(1, m.Extent() + 1)]


def slab(face, half: float):
    """The solid a face's wall fills: the face moved `half` back along its normal, then thickened 2 x half forward
    (OCC's simple offset, its side walls ruled along the normal)."""
    from OCP.BRepOffset import BRepOffset_MakeSimpleOffset

    back = ms.half_offset(face, half)
    back = ms._faces(back)[0] if ms._faces(back) else back
    mk = BRepOffset_MakeSimpleOffset(back, 2.0 * half)
    mk.SetBuildSolidFlag(True)
    mk.Perform()
    if not mk.IsDone():
        raise ValueError(f"a wall's slab could not be made ({mk.GetErrorMessage()})")
    out = mk.GetResultShape()
    # the offset's solid can come out turned inside out (its faces' normals inward): the mid-face is inside it
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_IN

    p = ms._inner_point(face)
    if p is not None and BRepClass3d_SolidClassifier(out, gp_Pnt(*map(float, p)), 1e-7).State() != TopAbs_IN:
        out = out.Reversed()
    return out


def _bop(kind: str, a, tools: list, fuzzy: float = 0.0):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.TopTools import TopTools_ListOfShape

    op = {"cut": BRepAlgoAPI_Cut, "common": BRepAlgoAPI_Common, "fuse": BRepAlgoAPI_Fuse}[kind]()
    args, tl = TopTools_ListOfShape(), TopTools_ListOfShape()
    args.Append(a)
    for t in tools:
        tl.Append(t)
    op.SetArguments(args)
    op.SetTools(tl)
    op.SetRunParallel(False)
    if fuzzy:
        op.SetFuzzyValue(fuzzy)
    op.Build()
    if not op.IsDone():
        raise ValueError(f"a Boolean {kind} failed")
    return op.Shape()


def sweep(face, dist: float, away_from):
    """The solid a face sweeps moved `dist` along its normal, on the side away from the solid `away_from` (OCC's
    simple offset, its side walls ruled along the normal)."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.BRepOffset import BRepOffset_MakeSimpleOffset
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_IN
    from OCP.TopoDS import TopoDS

    p = ms._inner_point(face)
    if p is None:
        raise ValueError("a face of a thick core has no inner point")
    # a copy: swept as itself, the core's own face became a wall of the sweep, and the fuse of the two came back empty
    face = TopoDS.Face_s(BRepBuilderAPI_Copy(face, True, False).Shape())
    for d in (dist, -dist):
        mk = BRepOffset_MakeSimpleOffset(face, d)
        mk.SetBuildSolidFlag(True)
        mk.Perform()
        if not mk.IsDone():
            continue
        out = mk.GetResultShape()
        from OCP.TopAbs import TopAbs_REVERSED

        n = np.asarray(ms._face_normal_at(face, p), float)
        if face.Orientation() == TopAbs_REVERSED:      # the offset goes along the face's own (oriented) normal
            n = -n
        q = gp_Pnt(*map(float, p + 0.5 * d * n))
        # the sweep must go out of the core; the offset's solid can come out turned inside out
        if BRepClass3d_SolidClassifier(away_from, q, 1e-7).State() == TopAbs_IN:
            continue
        if BRepClass3d_SolidClassifier(out, q, 1e-7).State() != TopAbs_IN:
            out = out.Reversed()
        return out
    raise ValueError("a face of a thick core could not be swept")


def grown(core, body, dist: float, tol: float):
    """MIXED-2: the core grown `dist` into the walls that run into it - each face of the core inside the body (where a
    wall's slab was taken off it: the wall's cross-section) swept `dist` along its outward normal, fused with the
    core. Only OCC's simple offset and Booleans, which repeat exactly: OCC's offset of the whole solid (rounded at its
    edges) handed back the same grown core with its faces' edges written differently run to run, and the mesh of the
    solid region differed (a stiffened panel's: 20,421 and 20,321 tetrahedra)."""
    skin = ms._compound(ms._faces(body))
    sweeps = []
    for f in ms._faces(core):
        p = ms._inner_point(f)
        if p is None or _near(skin, p, tol):
            continue
        sweeps.append(sweep(f, dist, core))
    if not sweeps:
        raise ValueError("a thick core has no face inside the body to grow from")
    return _bop("fuse", core, sweeps)


def _near(shape, p: np.ndarray, tol: float) -> bool:
    return ms._dist(ms._vertex(p), shape) <= tol


def split_body(body, kept: list[tuple[object, float, float]], thick_faces: list, *, scale: float) -> Split:
    """MIXED-1..3 on one solid `body` (its own units; `scale` mm per unit). `kept`: the body's sewn shell mid-faces,
    each (face, wall, offset: the face's distance from its wall's true middle, SHELL-7), units of the body.
    `thick_faces`: the body's faces the extraction called thick or solid. Never raises: a split that cannot be made
    says why (`ok` False)."""
    out = Split()
    try:
        _split(body, kept, thick_faces, scale, out)
    except Exception as exc:  # noqa: BLE001 - the body stays solid, with the reason
        logger.debug("mixed: the split failed (%s)", exc)
        out.ok, out.why = False, f"the thick regions could not be cut out ({exc})"
    return out


def _split(body, kept, thick_faces, scale: float, out: Split) -> None:
    out.body_volume = ms._volume(body)
    t_min = min(t for _f, t, _o in kept)
    tol = 1e-3 * t_min
    slabs = [slab(f, 0.5 * t * (1.0 + SLAB_SLACK) + abs(o)) for f, t, o in kept]
    left = _solids(_bop("cut", body, slabs))
    # MIXED-1: the cores - left-over pieces holding a thick face (a point inside one of the thick faces on the piece)
    marks = [p for p in (ms._inner_point(f) for f in thick_faces) if p is not None]
    cores, dropped = [], 0.0
    for piece in left:
        if any(_near(piece, p, tol) for p in marks):
            cores.append(piece)
        else:
            dropped += ms._volume(piece)
    out.dropped_volume = dropped
    if not cores:
        out.why = "no thick region is left once the shell walls are taken out"
        return
    if dropped > LEFT_OVER_MAX * out.body_volume:
        out.why = (f"{100 * dropped / out.body_volume:.0f}% of the metal is neither in a shell wall nor in a thick "
                   "region")
        return
    # MIXED-2: each core grown CUT_WALLS of the thickest wall running into it; the solid is the body's metal inside
    regions = []
    for core in cores:
        walls = [t for (f, t, _o) in kept if ms._dist(f, core) <= 0.5 * t * (1 + SLAB_SLACK) + tol + 0.5 * t]
        d = CUT_WALLS * (max(walls) if walls else t_min)
        out.cut_distance.append(d * scale)
        out.core_volumes.append(ms._volume(core) * scale ** 3)
        regions.append(grown(core, body, d, tol))
    zone = regions[0] if len(regions) == 1 else _bop("fuse", regions[0], regions[1:])
    solid = _solids(_bop("common", body, [zone]))
    if not solid:
        out.why = "the grown thick regions hold none of the body"
        return
    out.solid = solid[0] if len(solid) == 1 else ms._compound(solid)
    out.cores = len(cores)
    out.solid_volume = sum(ms._volume(s) for s in solid)
    # the trimmed mid-faces: the sewn surface less the grown cores, cut as ONE shape so the faces keep the edges they
    # share (cut face by face, an L bracket's two legs came back with an edge each where they meet: the shell mesh
    # hinged there, a seventh free mode); each piece is owned by the sewn face it lies on
    pieces = ms._faces(_bop("cut", ms._compound([f for f, _t, _o in kept]), [zone]))
    for p in pieces:
        q = ms._inner_point(p)
        if q is None:
            continue
        k = min(range(len(kept)), key=lambda i: (ms._dist(ms._vertex(q), kept[i][0]), i))
        t = kept[k][1]
        # every piece is kept, however narrow: the sewn surface holds junction strips half a wall wide (a stiffener's
        # mid-face carried down to the plate's mid-plane), and dropping them parted a stiffened panel's stiffeners
        # from its plate
        out.shell_faces.append(p)
        out.shell_from.append(k)
        out.shell_volume += ms._area(p) * t
    # the cut faces: the solid's faces inside the body (no point of theirs on the body's own skin)
    skin = ms._compound(ms._faces(body))
    for f in ms._faces(out.solid):
        p = ms._inner_point(f)
        if p is None or _near(skin, p, tol):
            continue
        walls = [t for (sf, t, _o) in kept if ms._dist(sf, f) <= tol + 0.5 * t]
        out.cut_faces.append(f)
        out.cut_walls.append(max(walls) if walls else t_min)
    held = (out.solid_volume + out.shell_volume) / out.body_volume if out.body_volume > 0 else 0.0
    if not VOLUME_BAND[0] <= held <= VOLUME_BAND[1]:
        out.why = f"the split holds {100 * held:.0f}% of the body's metal (outside {VOLUME_BAND})"
        return
    if not out.shell_faces:
        out.why = "no shell wall is left once the thick regions are cut out"
        return
    out.ok = True


__all__ = ["CUT_WALLS", "Split", "grown", "slab", "split_body"]
