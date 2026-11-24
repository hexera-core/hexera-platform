# Responsibility: Damage clean STEP parts in controlled, labelled, seed-reproducible ways for the CRS v4 benchmark.
# Boundaries: needs OCP (native tier). It labels damage; it does not judge it - the v4 checker does.
#
# Design 7.2: for an injected file "the original" used for fidelity is the DAMAGED INPUT (`InjectedCase.path`),
# never the clean source. The product never sees the clean source; it is consulted here only to label the
# defect (`label["clean_source"]`, `label["entities"]`), and `InjectedCase.fidelity_reference` says so.
#
# EVIDENCE STATUS (`KIND_STATUS`, also written to every label as `label["status"]`):
#   structural   the damage is a structural fault the re-read file shows directly (open shell, missing face, two
#                interpenetrating solids, a tiny feature, an IGES round trip). Verified in the native tests.
#   quarantined  loose_reapprox, tolerance_inflate, seam_variant, precision_truncate. After the STEP reader's own
#                healing, all four are one observable defect: a vertex or edge off its curve/surface, which the reader
#                answers by raising a tolerance. The files read back as valid solids; that is not shown to fail the
#                clean-STEP definition (layer 1 requires valid, no free edges, no small-edge errors; layer 3 measures
#                against the damaged input, where the injected deviation is invisible). INCLUSION IN THE BENCHMARK IS
#                GATED ON LAYER-2 EVIDENCE (a gmsh probe that actually fails on the injected file). Until then,
#                `DEFAULT_KINDS` excludes them and Task 8 must too.
#   infeasible   pcurve_corrupt: no damage survives the STEP reader (pinned by a canary test); inject() raises.
#   declined_stratum  face_delete_large: a face whose boundary loop spans MORE than the fill cap (limits.py) was
#                deleted. The damage is real and structural, but no op in the library may repair it BY DESIGN (the
#                faithful layer exists to stop `fill_loop` inventing most of a part), so the expected outcome is
#                "correctly_declined" (label["expected_outcome"]). It is generatable and may enter a pool, but the
#                report keeps it on its own line, never in clean gain, regressions or any clean rate.
#                `face_delete_inclined` is its sibling for a DIFFERENT reason: the loop is within the cap but the op's own
#                planar fill declines it (an inclined or curved face: the fill deviates from it by more than the fidelity
#                bound). Same stratum status, same expected outcome, its own kind so the reason stays on the label.
#                `face_delete` (the default kind) only deletes faces the op COULD put back: loop span <= the cap AND
#                the op's own `fill_loop` accepts the loop (measured by running it, see `_fill_restores`).
from __future__ import annotations

import json
import math
import random
import re
import tempfile
import warnings
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy, BRepBuilderAPI_Transform
from OCP.BRepGProp import BRepGProp
from OCP.BRepTools import BRepTools, BRepTools_ReShape
from OCP.GeomAbs import GeomAbs_C2, GeomAbs_Plane
from OCP.GeomAPI import GeomAPI_PointsToBSplineSurface
from OCP.gp import gp_Pnt, gp_Pnt2d, gp_Trsf, gp_Vec
from OCP.GProp import GProp_GProps
from OCP.IFSelect import IFSelect_RetDone
from OCP.IGESControl import IGESControl_Reader, IGESControl_Writer
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Reader, STEPControl_Writer
from OCP.TColgp import TColgp_Array2OfPnt
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_VERTEX
from OCP.TopExp import TopExp
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS, TopoDS_Compound, TopoDS_Shape
from OCP.TopTools import TopTools_IndexedMapOfShape

from meshpipeline.cad.repair_v4 import limits  # the op's own 5%: read through the module, never copied

KINDS: tuple[str, ...] = (
    "loose_reapprox", "unsew_perturb", "tolerance_inflate", "pcurve_corrupt", "seam_variant",
    "small_entities", "face_delete", "assembly_overlap", "precision_truncate", "iges_roundtrip",
    "face_delete_large", "face_delete_inclined",
)


KIND_STATUS: dict[str, str] = {
    "loose_reapprox": "quarantined", "unsew_perturb": "structural", "tolerance_inflate": "quarantined",
    "pcurve_corrupt": "infeasible", "seam_variant": "quarantined", "small_entities": "structural",
    "face_delete": "structural", "assembly_overlap": "structural", "precision_truncate": "quarantined",
    "iges_roundtrip": "structural", "face_delete_large": "declined_stratum",
    "face_delete_inclined": "declined_stratum",
}
#: The kinds whose correct outcome is a decline, and what the label says that outcome is.
DECLINED_STRATUM_KINDS: tuple[str, ...] = tuple(k for k in KINDS if KIND_STATUS[k] == "declined_stratum")
EXPECTED_OUTCOME: dict[str, str] = dict.fromkeys(DECLINED_STRATUM_KINDS, "correctly_declined")
QUARANTINED_KINDS: tuple[str, ...] = tuple(k for k in KINDS if KIND_STATUS[k] == "quarantined")
#: What a benchmark build should draw from by default: kinds whose damage is shown, not merely claimed.
DEFAULT_KINDS: tuple[str, ...] = tuple(k for k in KINDS if KIND_STATUS[k] == "structural")
#: What a pool may CONTAIN: the default kinds plus the labelled declined stratum (opt-in, never a default draw).
ADMITTED_KINDS: tuple[str, ...] = DEFAULT_KINDS + DECLINED_STRATUM_KINDS


class InjectionNotApplicable(ValueError):
    """This source has nothing this kind can damage (e.g. seam_variant on a part with no cylindrical seam)."""


class InjectionInfeasible(ValueError):
    """The kind can never produce damage that survives writing and re-reading. A sibling of InjectionNotApplicable,
    not a subclass: a caller that skips "nothing to damage in THIS source" must not skip this kind for every source."""


@dataclass(frozen=True)
class InjectedCase:
    path: Path
    kind: str
    label: dict[str, Any]

    @property
    def fidelity_reference(self) -> Path:
        """What fidelity is measured against: the damaged input itself, never the clean source (design 7.2)."""
        return self.path


# ---- shape helpers --------------------------------------------------------------------------------

def read_step(path: Path) -> TopoDS_Shape:
    reader = STEPControl_Reader()
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        raise ValueError(f"cannot read STEP: {path}")
    reader.TransferRoots()
    return reader.OneShape()


def write_step(shape: TopoDS_Shape, path: Path) -> None:
    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)
    if writer.Write(str(path)) != IFSelect_RetDone:
        raise ValueError(f"cannot write STEP: {path}")


def _entities(shape: TopoDS_Shape, kind: Any) -> list[TopoDS_Shape]:
    """Sub-shapes in OCCT's map order: deterministic for one shape, so an index names one entity."""
    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, kind, m)
    return [m.FindKey(i) for i in range(1, m.Extent() + 1)]


def _diag(shape: TopoDS_Shape) -> float:
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    return math.dist((x0, y0, z0), (x1, y1, z1))


def _center_area(face: TopoDS_Shape) -> tuple[list[float], float]:
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    c = props.CentreOfMass()
    return [round(c.X(), 6), round(c.Y(), 6), round(c.Z(), 6)], round(props.Mass(), 6)


def _edge_mid(edge: TopoDS_Shape) -> list[float]:
    props = GProp_GProps()
    BRepGProp.LinearProperties_s(edge, props)
    c = props.CentreOfMass()
    return [round(c.X(), 6), round(c.Y(), 6), round(c.Z(), 6)]


def _face_ref(shape: TopoDS_Shape, faces: Sequence[TopoDS_Shape], i: int) -> dict[str, Any]:
    center, area = _center_area(faces[i])
    return {"type": "face", "src_index": i, "center": center, "area": area}


def _edge_ref(edges: Sequence[TopoDS_Shape], i: int) -> dict[str, Any]:
    return {"type": "edge", "src_index": i, "center": _edge_mid(edges[i])}


def _copy(shape: TopoDS_Shape) -> TopoDS_Shape:
    return BRepBuilderAPI_Copy(shape, True).Shape()


def _pick(rng: random.Random, pool: Sequence[int], n: int) -> list[int]:
    return sorted(rng.sample(list(pool), min(n, len(pool))))


def _is_plane(face: TopoDS_Shape) -> bool:
    return BRepAdaptor_Surface(TopoDS.Face_s(face)).GetType() == GeomAbs_Plane


def _has_seam(face: TopoDS_Shape) -> bool:
    """A face that wraps round its own surface carries one edge twice: the seam."""
    f = TopoDS.Face_s(face)
    return any(BRep_Tool.IsClosed_s(TopoDS.Edge_s(e), f) for e in _entities(face, TopAbs_EDGE))


# ---- the ten injectors. Each returns (damaged shape or None, label fields); STEP-text kinds write themselves ----

@dataclass(frozen=True)
class _Ctx:
    src: Path      # the clean source: for labelling, and for the kinds that damage the file text
    out: Path      # where the damaged STEP goes


Injector = Callable[[TopoDS_Shape, random.Random, _Ctx], tuple[TopoDS_Shape | None, dict[str, Any]]]


def _loose_reapprox(shape, rng, work):
    faces = _entities(shape, TopAbs_FACE)
    candidates = [i for i, f in enumerate(faces) if not _is_plane(f) and not _has_seam(f)]
    if not candidates:
        raise InjectionNotApplicable("loose_reapprox needs a non-planar, non-periodic face")
    chosen = _pick(rng, candidates, 2)
    out = _copy(shape)
    copies = _entities(out, TopAbs_FACE)
    grid = 3 + rng.randrange(2)          # a coarse 3x3 / 4x4 sample, interpolated at degree 2: visibly loose
    builder = BRep_Builder()
    for i in chosen:
        face = TopoDS.Face_s(copies[i])
        surf = BRep_Tool.Surface_s(face)
        tol = BRep_Tool.Tolerance_s(face)
        u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
        pts = TColgp_Array2OfPnt(1, grid, 1, grid)
        for a in range(grid):
            for b in range(grid):
                pts.SetValue(a + 1, b + 1, surf.Value(u0 + (u1 - u0) * a / (grid - 1), v0 + (v1 - v0) * b / (grid - 1)))
        fit = GeomAPI_PointsToBSplineSurface(pts, 2, 2, GeomAbs_C2, 1.0).Surface()
        _fit_to_uv_box(fit, u0, u1, v0, v1)
        pcurves = []
        for edge in _entities(face, TopAbs_EDGE):
            pc = BRep_Tool.CurveOnSurface_s(TopoDS.Edge_s(edge), face, 0.0, 0.0)
            if pc is not None:
                pcurves.append((TopoDS.Edge_s(edge), pc))
        builder.UpdateFace(face, fit, TopLoc_Location(), tol)
        for edge, pc in pcurves:             # the edges keep their curves; the surface under them is what moved
            builder.UpdateEdge(edge, pc, fit, TopLoc_Location(), BRep_Tool.Tolerance_s(edge))
    return out, {"entities": [_face_ref(shape, faces, i) for i in chosen], "grid": grid}


def _fit_to_uv_box(fit, u0, u1, v0, v1) -> None:
    """The fit is parametrised on its own range; put it back on the face's UV box so old pcurves still apply."""
    uk, vk = fit.UKnots(), fit.VKnots()
    a0, a1 = uk.Value(uk.Lower()), uk.Value(uk.Upper())
    b0, b1 = vk.Value(vk.Lower()), vk.Value(vk.Upper())
    for k in range(uk.Lower(), uk.Upper() + 1):
        uk.SetValue(k, u0 + (u1 - u0) * (uk.Value(k) - a0) / (a1 - a0))
    for k in range(vk.Lower(), vk.Upper() + 1):
        vk.SetValue(k, v0 + (v1 - v0) * (vk.Value(k) - b0) / (b1 - b0))
    fit.SetUKnots(uk)
    fit.SetVKnots(vk)


def _unsew_perturb(shape, rng, work):
    faces = _entities(shape, TopAbs_FACE)
    diag = _diag(shape)
    moved = _pick(rng, range(len(faces)), max(1, len(faces) // 3))
    shell, builder = TopoDS.Shell_s(_new_shell()), BRep_Builder()
    shifts: dict[int, list[float]] = {}
    for i, face in enumerate(faces):
        piece = _copy(face)                      # a copy owns its own edges: no neighbour shares them any more
        if i in moved:
            d = [rng.uniform(-1, 1) for _ in range(3)]
            n = math.sqrt(sum(c * c for c in d)) or 1.0
            mag = diag * 2e-3 * rng.uniform(1.0, 2.0)
            shifts[i] = [round(c / n * mag, 9) for c in d]
            t = gp_Trsf()
            t.SetTranslation(gp_Vec(*shifts[i]))
            piece = BRepBuilderAPI_Transform(piece, t, True).Shape()
        builder.Add(shell, piece)
    return shell, {"entities": [{**_face_ref(shape, faces, i), "shift": shifts[i]} for i in moved],
                   "unsewn_faces": len(faces)}


def _new_shell() -> TopoDS_Shape:
    from OCP.TopoDS import TopoDS_Shell
    shell = TopoDS_Shell()
    BRep_Builder().MakeShell(shell)
    return shell


def _tolerance_inflate(shape, rng, work):
    # An edge's own tolerance does not survive STEP (the file carries no per-edge tolerance), and neither
    # does the declared model uncertainty (the reader ignores it). What does survive is the geometry a
    # cross-kernel export really leaves behind: a vertex that no longer sits on its curves. The reader
    # answers a vertex-to-curve gap by raising the tolerance to cover it. So the damage is that gap.
    vertices = _entities(shape, TopAbs_VERTEX)
    edges = _entities(shape, TopAbs_EDGE)
    chosen = _pick(rng, range(len(vertices)), max(2, len(vertices) // 4))
    out = _copy(shape)
    copies = _entities(out, TopAbs_VERTEX)
    diag, builder = _diag(shape), BRep_Builder()
    refs = []
    for i in chosen:
        d = [rng.uniform(-1, 1) for _ in range(3)]
        n = math.sqrt(sum(c * c for c in d)) or 1.0
        mag = diag * 1e-3 * rng.uniform(1.0, 2.0)
        v = TopoDS.Vertex_s(copies[i])
        p = BRep_Tool.Pnt_s(v)
        shift = [round(c / n * mag, 9) for c in d]
        builder.UpdateVertex(v, gp_Pnt(p.X() + shift[0], p.Y() + shift[1], p.Z() + shift[2]), mag * 1.01)
        q = BRep_Tool.Pnt_s(TopoDS.Vertex_s(vertices[i]))
        refs.append({"type": "vertex", "src_index": i, "center": [round(q.X(), 6), round(q.Y(), 6), round(q.Z(), 6)],
                     "shift": shift, "gap": round(mag, 9)})
    touched = sorted(j for j, e in enumerate(edges)
                     if any(TopoDS.Vertex_s(vertices[i]).IsSame(v) for i in chosen for v in _entities(e, TopAbs_VERTEX)))
    return out, {"entities": refs + [_edge_ref(edges, j) for j in touched], "min_gap": min(r["gap"] for r in refs)}


def _pcurve_corrupt(shape, rng, ctx):
    """INFEASIBLE through STEP, deliberately not shipped as a damager.

    Pcurves can be corrupted in memory (BRepCheck_Analyzer then says invalid) and the corrupted pcurve is written
    into the file. But OCCT's STEP reader rebuilds pcurves on import (its FromSTEP shape-processing sequence), so
    a default read returns the clean pcurves: a translated pcurve, a bowed one, a reversed one and a swapped one
    were each tried on a filleted box, and each read back valid with tolerance 1e-7, indistinguishable from the
    source. A file like that would sit in the benchmark as an unbroken "defect". The damage a pcurve fault leaves
    behind once healed is an edge that does not lie on its face; `loose_reapprox` and `seam_variant` inject that.
    """
    raise InjectionInfeasible(
        "pcurve_corrupt: the STEP reader rebuilds pcurves on import, so no pcurve damage survives a default read; "
        "use loose_reapprox or seam_variant for the edge-off-surface damage it would leave")


def _seam_variant(shape, rng, work):
    """Put the seam's 3D curve at a different angle than the cylinder's own seam: two exporters disagreeing on where
    the cylinder is cut. (Corrupting the seam's pcurves instead was tried: the STEP reader rebuilds them, so the damage
    never reaches a consumer.)"""
    from OCP.GeomAbs import GeomAbs_Cylinder
    from OCP.gp import gp_Ax1

    faces = _entities(shape, TopAbs_FACE)
    seamed = [i for i, f in enumerate(faces)
              if _has_seam(f) and BRepAdaptor_Surface(TopoDS.Face_s(f)).GetType() == GeomAbs_Cylinder]
    if not seamed:
        raise InjectionNotApplicable("seam_variant needs a cylindrical face with a seam")
    i = rng.choice(seamed)
    out = _copy(shape)
    face = TopoDS.Face_s(_entities(out, TopAbs_FACE)[i])
    seam = next(TopoDS.Edge_s(e) for e in _entities(face, TopAbs_EDGE) if BRep_Tool.IsClosed_s(TopoDS.Edge_s(e), face))
    axis = BRepAdaptor_Surface(face).Cylinder().Axis()
    angle = math.radians(rng.uniform(5.0, 20.0)) * rng.choice([-1, 1])
    first, last = BRep_Tool.Range_s(seam)
    curve = BRep_Tool.Curve_s(seam, 0.0, 0.0)     # location-resolved copy
    t = gp_Trsf()
    t.SetRotation(gp_Ax1(axis.Location(), axis.Direction()), angle)
    moved = curve.Transformed(t)
    builder = BRep_Builder()
    builder.UpdateEdge(seam, moved, TopLoc_Location(), BRep_Tool.Tolerance_s(seam))
    builder.Range(seam, first, last)
    seam_ref = {"type": "seam_edge", "face_src_index": i, "face_center": _center_area(faces[i])[0],
                "edge_center": _edge_mid(seam),
                "angle_rad": round(angle, 9)}
    return out, {"entities": [seam_ref], "variant": "seam_rotated"}


def _small_entities(shape, rng, work):
    from OCP.BRepClass import BRepClass_FaceClassifier
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2
    from OCP.TopAbs import TopAbs_IN

    faces = _entities(shape, TopAbs_FACE)
    planar = [i for i, f in enumerate(faces) if _is_plane(f)]
    if not planar:
        raise InjectionNotApplicable("small_entities needs a planar face to notch")
    diag = _diag(shape)
    radius, depth = diag * 1e-3 * rng.uniform(1.0, 2.0), diag * 2e-3
    for _ in range(200):
        i = rng.choice(planar)
        face = TopoDS.Face_s(faces[i])
        u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
        u, v = rng.uniform(u0, u1), rng.uniform(v0, v1)
        ok = all(BRepClass_FaceClassifier(face, gp_Pnt2d(u + du, v + dv), 1e-7).State() == TopAbs_IN
                 for du in (-3 * radius, 0.0, 3 * radius) for dv in (-3 * radius, 0.0, 3 * radius))
        if ok:
            break
    else:
        raise InjectionNotApplicable("small_entities found no room on any planar face")
    adaptor = BRepAdaptor_Surface(face)
    p, n = adaptor.Value(u, v), adaptor.Plane().Axis().Direction()
    cutter = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(p.X() - n.X() * depth, p.Y() - n.Y() * depth, p.Z() - n.Z() * depth), n), radius, 2 * depth).Shape()
    cut = BRepAlgoAPI_Cut(shape, cutter)
    if not cut.IsDone():
        raise InjectionNotApplicable("small_entities: the boolean cut failed on this shape")
    out = cut.Shape()
    before = {tuple(_center_area(f)[0]) for f in faces}
    tiny = [f for f in _entities(out, TopAbs_FACE) if _center_area(f)[1] < math.pi * (3 * radius) ** 2 + 1e-9
            and tuple(_center_area(f)[0]) not in before]
    refs = []
    for f in tiny:
        c, a = _center_area(f)
        refs.append({"type": "face", "src_index": None, "center": c, "area": a})
    if not refs:
        raise InjectionNotApplicable("small_entities: the cut produced no new small face")
    return out, {"entities": refs, "host_face": _face_ref(shape, faces, i), "radius": radius, "depth": depth,
                 "at": [round(p.X(), 6), round(p.Y(), 6), round(p.Z(), 6)]}


def _loop_span(part: TopoDS_Shape, face: TopoDS_Shape) -> tuple[float, float]:
    """(loop span, part diagonal) of `face`'s boundary loop. The deleted face's boundary loop as a fraction of the part diagonal, measured the way `fill_loop` measures
    it (ops/faces.py `_fill_loop`): the bounding box of the loop's edges, against the bounding-box diagonal of the
    part the op is GIVEN (so pass the damaged part when re-checking)."""
    box = Bnd_Box()
    for edge in _entities(face, TopAbs_EDGE):
        BRepBndLib.Add_s(edge, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    return math.dist((x0, y0, z0), (x1, y1, z1)), _diag(part)


def _fill_restores(damaged: TopoDS_Shape) -> tuple[bool, str, dict]:
    """Would the op put the deleted face back FAITHFULLY? Runs `fill_loop` itself (the kernel function ops/faces.py
    registers, continuity G0) on the damaged part: its single-closed-loop rule decides whether it CAN fill, and the
    fill-to-surroundings deviation it MEASURES, against the fidelity bound it reports beside it, decides whether the
    fill is faithful. (Until the owner decision of 2026-11-24 the op declined an unfaithful fill itself; it now fills
    and reports, so this function applies the bound to the op's own measurement to keep the strata's meaning: a
    `face_delete_inclined` loop is one a planar fill cannot restore within the bound.) Returns (accepted, reason,
    measured).

    NOT a geometric proxy, so there is no proxy error to bound. The one difference from the real run is that the
    part is measured from the in-memory shape written to BREP, not after the STEP round trip the search reads; STEP
    prints coordinates to ~1e-6 mm, so a loop is misclassified only if its deviation sits within ~1e-5 mm of the
    bound (0.1 mm), and the label records the measured number so such a case is visible."""
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    import meshpipeline.cad.repair_v4.ops  # noqa: F401  (registers the ops; ws_kernel imported alone is circular)
    from meshpipeline.cad.repair.identity import quantum_for
    from meshpipeline.cad.repair_v4 import ws_kernel
    from meshpipeline.cad.repair_v4.ops import faces as face_ops

    with tempfile.TemporaryDirectory(prefix="fill_probe_") as td:
        before, after = str(Path(td) / "damaged.brep"), str(Path(td) / "filled.brep")
        ws_kernel.write_brep(damaged, before)
        reread = ws_kernel.read_brep(before)  # indices are those of the FILE the op will read
        ancestors = TopTools_IndexedDataMapOfShapeListOfShape()
        TopExp.MapShapesAndAncestors_s(reread, TopAbs_EDGE, TopAbs_FACE, ancestors)
        edge_index = ws_kernel._maps(reread)["edge"]
        free = [edge_index.FindIndex(ancestors.FindKey(i)) for i in range(1, ancestors.Extent() + 1)
                if ancestors.FindFromIndex(i).Size() == 1 and not BRep_Tool.Degenerated_s(TopoDS.Edge_s(ancestors.FindKey(i)))]
        if not free:
            return False, "no free edge: the deletion left the part closed", {}
        answer = face_ops._fill_loop({"brep_path": before, "out_path": after, "edges": free, "continuity": "G0",
                                      "quantum": quantum_for(ws_kernel.diagonal_of(reread))})
    if "declined" in answer:
        return False, str(answer["declined"]), {}
    measured = dict(answer.get("params", {}).get("measured", {}))
    kept = {k: measured[k] for k in ("fill_to_surroundings", "bound_mm", "fill_surface") if k in measured}
    dev, bound = measured.get("fill_to_surroundings"), measured.get("bound_mm")
    if dev is not None and bound is not None and float(dev) > float(bound):
        return False, (f"over_bound (measured): the fill deviates {float(dev):.6g} mm from its surroundings, beyond "
                       f"the fidelity bound {float(bound):.6g} mm"), kept
    return True, "accepted", kept


def _delete_face(shape, rng, mode: str):
    """Delete one face. `mode`:
      "fixable"  (face_delete)           loop within the cap AND the op's own fill accepts it: a small face lost in
                                         translation that the library is built to put back.
      "large"    (face_delete_large)     loop beyond the cap (limits.fill_span_allowed): declined by design.
      "inclined" (face_delete_inclined)  loop within the cap but the op's fill declines it (inclined or curved face: a
                                         planar fill deviates from it by more than the fidelity bound).
    The cap is `limits`' one 5%; the deviation test is the op's own run (`_fill_restores`)."""
    faces = _entities(shape, TopAbs_FACE)
    name = {"fixable": "face_delete", "large": "face_delete_large", "inclined": "face_delete_inclined"}[mode]
    verdicts: dict[int, tuple[bool, str, dict]] = {}

    def damaged_without(i: int) -> TopoDS_Shape:
        reshape = BRepTools_ReShape()
        reshape.Remove(faces[i])
        return reshape.Apply(shape)

    chosen: list[int] = []
    for i, f in enumerate(faces):
        within = limits.fill_span_allowed(*_loop_span(shape, f))
        if mode == "large":
            if not within:
                chosen.append(i)
            continue
        if not within:
            continue
        verdicts[i] = _fill_restores(damaged_without(i))
        if verdicts[i][0] is (mode == "fixable"):
            chosen.append(i)
    if not chosen:
        raise InjectionNotApplicable(
            f"{name}: no face has a boundary loop {'above' if mode == 'large' else 'within'} "
            f"{100 * limits.MAX_FILL_SPAN_RATIO:g}% of the part diagonal"
            + ("" if mode == "large" else f" that the op's own fill_loop {'accepts' if mode == 'fixable' else 'declines'}"))
    i = rng.choice(chosen)
    out = damaged_without(i)
    if len(_entities(out, TopAbs_FACE)) != len(faces) - 1:
        raise InjectionNotApplicable(f"{name}: the face could not be removed from this shape")
    span, diagonal = _loop_span(out, faces[i])  # re-measured against the damaged part, as the op will see it
    ratio = span / diagonal
    if limits.fill_span_allowed(span, diagonal) is (mode == "large"):
        raise InjectionNotApplicable(
            f"{name}: deleting the face moved the part diagonal across the {100 * limits.MAX_FILL_SPAN_RATIO:g}% line "
            f"(loop span {100 * ratio:.2f}% of the damaged part's diagonal)")
    label = {"entities": [_face_ref(shape, faces, i)], "loop_span_ratio": round(ratio, 6),
             "fill_cap_ratio": limits.MAX_FILL_SPAN_RATIO}
    if mode != "large":
        accepted, reason, measured = verdicts[i]
        label["fill_loop_accepts"] = accepted
        label.update({f"fill_{k}": v for k, v in measured.items()})
        if not accepted:
            label["fill_loop_declined"] = reason[:300]
    return out, label


def _face_delete(shape, rng, work):
    return _delete_face(shape, rng, "fixable")


def _face_delete_large(shape, rng, work):
    return _delete_face(shape, rng, "large")


def _face_delete_inclined(shape, rng, work):
    return _delete_face(shape, rng, "inclined")


def _assembly_overlap(shape, rng, work):
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    extents = [x1 - x0, y1 - y0, z1 - z0]
    axis = rng.randrange(3)
    shift = [0.0, 0.0, 0.0]
    shift[axis] = extents[axis] * rng.uniform(0.15, 0.35) * rng.choice([-1, 1])   # less than the part's width: they interpenetrate
    t = gp_Trsf()
    t.SetTranslation(gp_Vec(*shift))
    twin = BRepBuilderAPI_Transform(_copy(shape), t, True).Shape()
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(BRepAlgoAPI_Common(shape, twin).Shape(), props)
    compound, builder = TopoDS_Compound(), BRep_Builder()
    builder.MakeCompound(compound)
    builder.Add(compound, shape)
    builder.Add(compound, twin)
    bodies = [{"type": "body", "body_index": 0, "role": "original"},
              {"type": "body", "body_index": 1, "role": "translated_copy", "shift": [round(c, 9) for c in shift]}]
    return compound, {"entities": bodies, "overlap_volume": round(props.Mass(), 6)}


_REAL = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
# A whole statement, whichever way the exporter wrapped it across lines.
_TUPLE_STMT = re.compile(r"(#(\d+)\s*=\s*(CARTESIAN_POINT|DIRECTION)\s*\(\s*'[^']*'\s*,\s*\()([^()]*)(\)\s*\)\s*;)")


def _truncate(x: float, places: int) -> str:
    f = 10 ** places
    return f"{math.trunc(x * f) / f:.{places}f}"


def _precision_truncate(shape, rng, ctx):
    """Re-export with the coordinates cut to few decimals, as a lossy exporter or a hand-trimmed file does."""
    quantum = _diag(shape) * 2e-3
    places = max(0, math.ceil(-math.log10(quantum))) + rng.choice([0, 1])
    changed: list[dict[str, Any]] = []

    def cut(m: re.Match[str]) -> str:
        head, ident, entity, body, tail = m.groups()
        values = re.findall(_REAL, body)
        if len(values) != 3:                 # 2D parameter-space points are healed on read; only model-space ones matter
            return m.group(0)
        p = places if entity == "CARTESIAN_POINT" else places + 2   # a direction cut as hard as a point is not a direction
        new_body = ",".join(_truncate(float(x), p) for x in values)
        if [float(x) for x in new_body.split(",")] == [float(x) for x in values]:
            return m.group(0)
        changed.append({"type": "step_entity", "id": f"#{ident}", "entity": entity})
        return f"{head}{new_body}{tail}"

    ctx.out.write_text(_TUPLE_STMT.sub(cut, ctx.src.read_text()))
    return None, {"entities": changed, "decimals": places, "scope": "whole_file"}


def _iges_roundtrip(shape, rng, ctx):
    """Send the part through IGES and back. IGES carries trimmed surfaces, not a sewn solid, at its own precision."""
    writer = IGESControl_Writer("MM", 0)
    writer.AddShape(shape)
    writer.ComputeModel()
    igs = ctx.out.with_suffix(".igs")
    if not writer.Write(str(igs)):
        raise ValueError("IGES write failed")
    reader = IGESControl_Reader()
    if reader.ReadFile(str(igs)) != IFSelect_RetDone:
        raise ValueError("IGES read failed")
    reader.TransferRoots()
    faces = _entities(shape, TopAbs_FACE)
    return reader.OneShape(), {"entities": [_face_ref(shape, faces, i) for i in range(len(faces))],
                               "scope": "whole_file", "iges_file": str(igs)}


_INJECTORS: dict[str, Injector] = {
    "loose_reapprox": _loose_reapprox,
    "unsew_perturb": _unsew_perturb,
    "tolerance_inflate": _tolerance_inflate,
    "pcurve_corrupt": _pcurve_corrupt,
    "seam_variant": _seam_variant,
    "small_entities": _small_entities,
    "face_delete": _face_delete,
    "face_delete_large": _face_delete_large,
    "face_delete_inclined": _face_delete_inclined,
    "assembly_overlap": _assembly_overlap,
    "precision_truncate": _precision_truncate,
    "iges_roundtrip": _iges_roundtrip,
}


def inject(src_step: Path | str, kind: str, seed: int, out_dir: Path | None = None) -> InjectedCase:
    """Damage `src_step` with `kind`; the same source, kind and seed always give the same damage."""
    if kind not in _INJECTORS:
        raise ValueError(f"unknown injector kind {kind!r}; known: {', '.join(KINDS)}")
    src = Path(src_step)
    out_dir = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="v4-inject-"))
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{src.stem}.{kind}.{seed}.step"
    rng = random.Random(f"{kind}:{seed}")
    shape = read_step(src)
    damaged, fields = _INJECTORS[kind](shape, rng, _Ctx(src=src, out=out))
    if damaged is not None:
        write_step(damaged, out)
    # Post-conditions: an OCCT call that quietly did nothing must not leave a clean file labelled as damaged.
    if not fields.get("entities"):
        out.unlink(missing_ok=True)
        raise InjectionNotApplicable(f"{kind}: nothing in {src.name} was damaged (no entity to name)")
    if out.read_bytes() == src.read_bytes():
        out.unlink(missing_ok=True)
        raise InjectionNotApplicable(f"{kind}: the damaged file is identical to {src.name}")
    label = {
        "kind": kind, "seed": seed, "status": KIND_STATUS[kind], "clean_source": str(src),
        "fidelity_reference": "damaged_input",     # design 7.2: the clean source only labels the defect
        **({"expected_outcome": EXPECTED_OUTCOME[kind]} if kind in EXPECTED_OUTCOME else {}),
        **fields,
    }
    return InjectedCase(path=out, kind=kind, label=label)


# ---- calibration against the real failure histogram ------------------------------------------------

#: gmsh error text -> failure family, in match order. The families are those of mesh-failure-real.json.
_FAMILIES: tuple[tuple[str, str], ...] = (
    ("overlapping facets", "overlapping_facets"),
    ("PLC Error", "plc_intersection"),
    ("timeout", "timeout_crash"),
    ("crash", "timeout_crash"),
    ("periodic surface", "periodic_surface"),
    ("closed loop", "open_loop"),
    ("no volume elements", "no_volume"),
    ("Could not recover boundary", "boundary_recovery"),
)

#: What each kind is EXPECTED to fail as in gmsh. A prior, not a measurement: the re-probe (Task 8) runs gmsh
#: on the injected files and records the observed family in `label["observed_family"]`, which wins.
KIND_FAMILY_PRIOR: dict[str, str] = {
    "unsew_perturb": "open_loop", "face_delete": "no_volume", "assembly_overlap": "plc_intersection",
    "small_entities": "overlapping_facets", "iges_roundtrip": "open_loop",
}   # quarantined kinds are one observable defect (vertex off curve) and have no basis for a family: none given


def failure_family(gmsh_error: str) -> str:
    for needle, family in _FAMILIES:
        if needle in gmsh_error:
            return family
    return "other"


def target_histogram(corpus: Path | str) -> dict[str, float]:
    """Failure-family shares of mesh-failure-real.json (sums to 1)."""
    files = json.loads(Path(corpus).read_text())["files"]
    counts = Counter(failure_family(f["gmsh_error"]) for f in files)
    total = sum(counts.values())
    return {family: n / total for family, n in sorted(counts.items())}


def _case_family(case: InjectedCase) -> str:
    if case.kind in DECLINED_STRATUM_KINDS:
        raise ValueError(f"{case.kind} is the declined stratum (expected outcome {EXPECTED_OUTCOME[case.kind]!r}): it is "
                         "reported on its own line and never weighted into the failure-family calibration")
    observed = case.label.get("observed_family")
    if observed:
        return str(observed)
    if case.kind not in KIND_FAMILY_PRIOR:
        raise ValueError(f"{case.kind}: no observed_family on the case and no prior for the kind; run the gmsh re-probe")
    return KIND_FAMILY_PRIOR[case.kind]


class Calibration(dict):
    """kind -> weight, plus what the weights are worth: `basis` ("observed", "prior" or "mixed"), the target share
    that no case reaches (`dropped_mass`, over `dropped_families`) and the family each kind was placed in."""

    basis: str
    dropped_mass: float
    dropped_families: dict[str, float]
    kind_family: dict[str, str]


def calibrate(cases: Sequence[InjectedCase], target_hist: dict[str, float]) -> Calibration:
    """Weight each kind so that a weighted draw of `cases` follows `target_hist` over the families the cases reach.

    A kind sits in the family most of its cases were OBSERVED in (`label["observed_family"]`, from the gmsh re-probe);
    without that it falls back to `KIND_FAMILY_PRIOR`, a guess, and a warning says so. A family's target share is split
    evenly among the kinds in it. Families no case reaches cannot be matched: their share is NOT silently absorbed; it is
    reported as `dropped_mass` and the rest renormalised, so read `basis` and `dropped_mass` before trusting a match.
    """
    by_kind: dict[str, Counter[str]] = {}
    observed = prior = 0
    for c in cases:
        by_kind.setdefault(c.kind, Counter())[_case_family(c)] += 1
        if c.label.get("observed_family"):
            observed += 1
        else:
            prior += 1
    if prior:
        warnings.warn(f"calibrate: {prior} of {observed + prior} cases have no observed_family; their family is a prior "
                      "guess, not a measurement", stacklevel=2)
    kind_family = {k: sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] for k, cnt in by_kind.items()}
    members: dict[str, list[str]] = {}
    for kind, family in sorted(kind_family.items()):
        members.setdefault(family, []).append(kind)
    reachable = {f: target_hist.get(f, 0.0) for f in members}
    total = sum(reachable.values())
    if total <= 0:
        raise ValueError("no injected kind reaches any family with weight in the target histogram")
    result = Calibration({kind: reachable[family] / total / len(kinds) for family, kinds in members.items() for kind in kinds})
    result.basis = "observed" if not prior else ("prior" if not observed else "mixed")
    result.dropped_families = {f: v for f, v in target_hist.items() if f not in members and v > 0}
    result.dropped_mass = sum(result.dropped_families.values())
    result.kind_family = kind_family
    return result
