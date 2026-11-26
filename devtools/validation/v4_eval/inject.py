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
    # CRS v5 step 5 (closed_periodic_face, invalid_brep_tolerance)
    "periodic_no_seam", "unbounded_plane", "pole_degenerate", "units_scale", "bad_spline",
    # CRS v5 step 6 (the conformal assembly pass)
    "misalignment", "duplicate_body",
    # CRS v5 step 7 (close_faces)
    "shaft_in_bore", "thin_wall",
    # CRS v5 step 10 (low_quality)
    "floating_solid", "sliver_face",
    # CRS v5 step 9 (surface_crossing)
    "duplicate_faces", "true_crossing", "edge_through_face",
    # CRS v5 step 8 (overlap_self_intersection)
    "self_intersecting_face",
    # CRS v5 step 11 (open_shell_volume)
    "inside_out", "non_manifold_edge", "sheet_body",
    # CRS v5 round 2 (facet_crossing_local)
    "knife_lens",
)


KIND_STATUS: dict[str, str] = {
    "loose_reapprox": "quarantined", "unsew_perturb": "structural", "tolerance_inflate": "quarantined",
    # PROMOTED (v5 step 5): gmsh_alt fails `periodic_surface` on the injected file (KIND_EVIDENCE)
    "pcurve_corrupt": "infeasible", "seam_variant": "structural", "small_entities": "structural",
    "face_delete": "structural", "assembly_overlap": "structural", "precision_truncate": "quarantined",
    "iges_roundtrip": "structural", "face_delete_large": "declined_stratum",
    "face_delete_inclined": "declined_stratum",
    # CRS v5 step 5: structural only where a checker or mesher was shown to fail on the injected file (the same
    # evidence rule as above); `KIND_EVIDENCE` records what the probe measured, promotion or not.
    "periodic_no_seam": "quarantined", "pole_degenerate": "structural", "units_scale": "quarantined",
    "bad_spline": "structural",
    # layer 1 fails it (unbounded_geometry), but the clean definition puts unbounded parts OUT OF SCOPE (the arms
    # exclude them, `arms._usable`): never a default draw, exercised directly against trim_unbounded only
    "unbounded_plane": "quarantined",
    # CRS v5 step 6 (KIND_EVIDENCE): layer 1 fails duplicate_body; nothing fails a misalignment, so it is report-only
    "misalignment": "quarantined", "duplicate_body": "structural",
    # CRS v5 step 7 (KIND_EVIDENCE): a thin wall fails the meshers; a shaft in a bore (two bodies) does not
    "shaft_in_bore": "quarantined", "thin_wall": "structural",
    # CRS v5 step 10 (KIND_EVIDENCE): all four meshers fail sliver_face; nothing fails a floating tiny solid
    "floating_solid": "quarantined", "sliver_face": "structural",
    # CRS v5 step 9 (KIND_EVIDENCE): layer 1 fails the two crossings; duplicate faces mesh clean, so report-only
    "duplicate_faces": "quarantined", "true_crossing": "structural", "edge_through_face": "structural",
    # CRS v5 step 8 (KIND_EVIDENCE): layer 1 reports self_intersection and all four meshers fail on 1 of 4 sources
    "self_intersecting_face": "structural",
    # CRS v5 step 11 (KIND_EVIDENCE); sheet_body is unrepairable by design (owner: no local re-surfacing)
    "inside_out": "quarantined", "non_manifold_edge": "quarantined", "sheet_body": "declined_stratum",
    # CRS v5 round 2 (KIND_EVIDENCE): gmsh_default fails it on 2 of 3 sources
    "knife_lens": "structural",
}
#: CRS v5 step 5: what the jig probe (v4 full verdict on the injected file) measured for each probed kind, so a status
#: is never claimed without its evidence. Filled from the probe run; a kind not here has not been probed.
KIND_EVIDENCE: dict[str, str] = {
    "pole_degenerate": "3/3 sources fail: layer 1 brep_invalid (+self_intersection on 1), gmsh import_failed on all "
                       "three gmsh members, netgen fails on 2",
    "seam_variant": "1/3 sources fail: gmsh_alt periodic_surface on 00607178 (2/3 mesh clean, large_tolerance soft "
                    "only); promoted on that one demonstrated failure",
    "bad_spline": "1/3 sources fail: netgen_occ timed out twice on 00561866 (840 s); 2/3 mesh clean",
    "unbounded_plane": "3/3 sources: layer 1 unbounded_geometry (the bounded +-5e9 mm plane; an infinite face was "
                       "dropped by the STEP writer and read back clean, 3/3)",
    "periodic_no_seam": "3/3 sources mesh clean on all four meshers: the STEP reader restores the seam",
    "units_scale": "3/3 sources mesh clean on all four meshers at 1/1000 scale: not a failure, so report only",
    "tolerance_inflate": "4/4 sources mesh clean (large_tolerance soft only): stays quarantined",
    "loose_reapprox": "not probed: 0/6 sources had a curved face without a seam",
    "duplicate_body": "3/3 sources fail: layer 1 body_overlap (jig probe, v5 step 6)",
    "misalignment": "3/3 sources mesh clean on all four meshers, layer 1 valid, at a gap of 0.4x the fidelity bound "
                    "(0.040 mm) and again at 1e-4..1e-3x (jig probe, v5 step 6): two parallel faces a hair apart "
                    "fail nothing, so it stays quarantined",
    "thin_wall": "3/3 sources fail (seed 1, jig, v5 step 7): 1, 1 and 2 of 4 panel members pass on 00511922, 00524864, "
                 "00525435 (gmsh_prod `other`, gmsh_default/alt crossing facets); the predictor finds the tube's two "
                 "walls crossing at all three gmsh members",
    "shaft_in_bore": "3/3 sources mesh clean on all four meshers (seed 1, jig, v5 step 7): facets of two DIFFERENT "
                     "bodies crossing do not fail a per-volume mesher, so it stays quarantined",
    "sliver_face": "3/3 sources fail at a step 1e-5..2e-5 x diagonal deep: low_quality on gmsh_prod/default/alt, netgen "
                   "low_quality (2) or crash (1), layer 1 soft small_edge (VM probe, v5 step 10). At 1e-3..2e-3 x "
                   "diagonal all 3 meshed clean, so the injected depth is the failing one",
    "floating_solid": "3/3 sources mesh clean on all four meshers at an edge of 1e-3..2e-3 and again 1e-5..2e-5 x "
                      "diagonal (layer 1 soft small_edge/small_face only, VM probe, v5 step 10): quarantined",
    "small_entities": "re-probed in v5 step 10: 3/3 of the step-6 sources meshed clean (status kept: it was promoted on "
                      "earlier evidence)",
    "duplicate_faces": "3/3 sources clean (layer 1 valid, all four meshers pass) with both halves' cut faces coincident "
                       "and unshared (GCP probe, v5 step 9): the meshers' fragment conformalises them, so it stays "
                       "quarantined",
    "true_crossing": "3/3 sources fail: layer 1 body_overlap (the tilted cut faces' wedges); netgen_occ also fails on "
                     "3/3 (netgen_meshing_failed 1, low_quality 2) (GCP probe, v5 step 9)",
    "edge_through_face": "3/3 sources fail: layer 1 body_overlap (the fin sunk through a planar face) (GCP probe, "
                         "v5 step 9)",
    "self_intersecting_face": "4/4 sources fail layer 1 (VM probe, v5 step 8). On 00560521 it is the kind's own defect: "
                              "brep_invalid + non_positive_volume + self_intersection, and all four meshers fail "
                              "(other, edge_through_face, no_volume, netgen_meshing_failed); promoted on it. On the "
                              "other 3 the STEP reader splits the crossing face set into two solids, so layer 1 reads "
                              "body_overlap (the same class) and the panel passes",
    "inside_out": "3/3 sources read back layer-1 valid: the STEP reader re-orients a reversed closed solid, so the "
                  "damage does not survive the file (local probe, v5 step 11); exercised on BREP in the unit tests",
    "non_manifold_edge": "1/3 sources injectable (2/3 have no convex straight edge between planar faces); the BREP "
                         "carries 1 non-manifold edge, the STEP round trip reads back layer-1 valid (local probe, v5 "
                         "step 11): quarantined; exercised on BREP in the unit tests",
    "knife_lens": "2/3 sources fail (seed 1, VM probe, v5 round 2): gmsh_default overlapping_facets on 00615057 (the "
                  "predictor: the lens's arc and chord faces crossing across their knife edge) and on 00551787 (2 of 4 "
                  "members pass; the staged healer's output meshes clean); 00607513 meshes clean. Not repaired by the "
                  "class: split_angle cannot cut a ~20 degree arc (its floor is 30)",
    "sheet_body": "3/3 sources fail layer 1 open_shell on the free sheet, and the open_shell_volume detector calls "
                  "all 3 a genuine sheet body (unrepairable, local probe, v5 step 11)",
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


#: misalignment's gap, as a share of the fidelity bound min(0.1 mm, 1e-3 x diagonal): well inside the line the
#: conformal pass calls a modelling slip (so `tweak` may close it), and far above the kernel's 1e-7 mm contact
_MISALIGN_SHARE = (0.05, 0.5)


def _halves(shape, rng):
    """(the part cut in two by a plane across its longest extent at a random position in its middle half, the axis,
    the cut position), or (None, axis, at) when a side is empty. Each half is a Common with a padded box, so the two
    cut faces are separate faces on the same plane."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    lo, hi = list(box.Get()[:3]), list(box.Get()[3:])
    axis = max(range(3), key=lambda k: hi[k] - lo[k])
    at = lo[axis] + (hi[axis] - lo[axis]) * rng.uniform(0.25, 0.75)
    pad = 0.1 * _diag(shape) + 1.0
    low_hi, high_lo = list(hi), list(lo)
    low_hi[axis], high_lo[axis] = at, at
    halves = []
    for a, b in (([c - pad for c in lo], [c + pad if k != axis else low_hi[k] for k, c in enumerate(hi)]),
                 ([c - pad if k != axis else high_lo[k] for k, c in enumerate(lo)], [c + pad for c in hi])):
        piece = BRepAlgoAPI_Common(shape, BRepPrimAPI_MakeBox(gp_Pnt(*a), gp_Pnt(*b)).Shape()).Shape()
        if not _entities(piece, TopAbs_FACE):
            return None, axis, at
        halves.append(piece)
    return halves, axis, at


def _misalignment(shape, rng, work):
    """Two bodies a hair apart: the part cut in two by a plane across its longest extent (a random position in the
    middle half), and one half translated along the plane's normal by a gap far under the fidelity bound - the two
    cut faces are parallel, near-coincident, and do NOT touch. An assembly exported with one part's placement off by
    a rounding slip."""
    halves, axis, at = _halves(shape, rng)
    if halves is None:
        return None, {}
    bound = min(0.1, 1e-3 * _diag(shape))
    gap = bound * rng.uniform(*_MISALIGN_SHARE)
    shift = [0.0, 0.0, 0.0]
    shift[axis] = gap
    t = gp_Trsf()
    t.SetTranslation(gp_Vec(*shift))
    moved = BRepBuilderAPI_Transform(halves[1], t, True).Shape()
    compound, builder = TopoDS_Compound(), BRep_Builder()
    builder.MakeCompound(compound)
    builder.Add(compound, halves[0])
    builder.Add(compound, moved)
    bodies = [{"type": "body", "body_index": 0, "role": "low_half"},
              {"type": "body", "body_index": 1, "role": "high_half_moved", "shift": [round(c, 12) for c in shift]}]
    return compound, {"entities": bodies, "gap_mm": round(gap, 12), "axis": axis, "cut_at": round(at, 9),
                      "fidelity_bound_mm": round(bound, 9)}


def _duplicate_body(shape, rng, work):
    """The part and an exact copy of it in the same place: an exporter that wrote one body twice."""
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    compound, builder = TopoDS_Compound(), BRep_Builder()
    builder.MakeCompound(compound)
    builder.Add(compound, shape)
    builder.Add(compound, _copy(shape))
    bodies = [{"type": "body", "body_index": 0, "role": "original"},
              {"type": "body", "body_index": 1, "role": "duplicate"}]
    return compound, {"entities": bodies, "overlap_volume": round(abs(props.Mass()), 6)}


#: shaft_in_bore / thin_wall: the bore's radius as a share of the part's diagonal, and the radial gap (or wall) as a
#: share of that radius - f9501251fb30's measured shaft-in-bore is 0.127 mm on 3.175 mm (0.04)
_CLOSE_RADIUS_OF_DIAG = 0.05
_CLOSE_GAP_OF_RADIUS = (0.02, 0.05)


def _beside(shape: TopoDS_Shape, r: float) -> tuple[list[float], float]:
    """Where a close-faces fixture goes: on the part's far x side, clear of it by a tenth of the diagonal, so it
    overlaps nothing (the defect is the close pair, never a body overlap). (base point, height)."""
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    d = _diag(shape)
    return [x1 + 0.1 * d + 2.0 * r, 0.5 * (y0 + y1), z0], max(0.25 * d, 4.0 * r)


def _cylinder(base: list[float], r: float, h: float, turn: float) -> TopoDS_Shape:
    """A z-axis cylinder at `base`, its seam turned `turn` radians about its axis (two coaxial faces meshed in their
    own frames put their polygon vertices on different rays - the crossing the class repairs)."""
    import math as _m

    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir

    ax = gp_Ax2(gp_Pnt(*base), gp_Dir(0, 0, 1), gp_Dir(_m.cos(turn), _m.sin(turn), 0))
    return BRepPrimAPI_MakeCylinder(ax, r, h).Shape()


def _close_fixture(shape, rng, *, thin: bool):
    import math as _m

    r = _CLOSE_RADIUS_OF_DIAG * _diag(shape)
    gap = r * rng.uniform(*_CLOSE_GAP_OF_RADIUS)
    base, h = _beside(shape, r)
    # half of gmsh's coarsest step (Mesh.MinimumCirclePoints 7: six segments) give or take: the worst misalignment
    turn = (_m.pi / 6.0) * rng.uniform(0.6, 1.0)
    compound, builder = TopoDS_Compound(), BRep_Builder()
    builder.MakeCompound(compound)
    builder.Add(compound, shape)
    if thin:
        tube = BRepAlgoAPI_Cut(_cylinder(base, r, h, 0.0),
                               _cylinder([base[0], base[1], base[2] - h], r - gap, 3 * h, turn)).Shape()
        builder.Add(compound, tube)
        bodies = [{"type": "body", "body_index": 0, "role": "original"},
                  {"type": "body", "body_index": 1, "role": "thin_walled_tube", "outer_radius": round(r, 9),
                   "inner_radius": round(r - gap, 9)}]
        return compound, {"entities": bodies, "wall_mm": round(gap, 9), "seam_turn_rad": round(turn, 9)}
    bush = BRepAlgoAPI_Cut(_cylinder(base, 2.0 * r, h, 0.0),
                           _cylinder([base[0], base[1], base[2] - h], r, 3 * h, 0.0)).Shape()
    shaft = _cylinder([base[0], base[1], base[2] + 0.1 * h], r - gap, 0.8 * h, turn)
    builder.Add(compound, bush)
    builder.Add(compound, shaft)
    bodies = [{"type": "body", "body_index": 0, "role": "original"},
              {"type": "body", "body_index": 1, "role": "bushing", "bore_radius": round(r, 9)},
              {"type": "body", "body_index": 2, "role": "shaft", "radius": round(r - gap, 9)}]
    return compound, {"entities": bodies, "gap_mm": round(gap, 9), "seam_turn_rad": round(turn, 9)}


def _shaft_in_bore(shape, rng, work):
    """A shaft in a bore a small radial gap apart, their seams turned: beside the part, a bushing (one body) and a
    shaft (another), coaxial, never touching. An assembly with a running clearance, as f9501251fb30 has."""
    return _close_fixture(shape, rng, thin=False)


def _thin_wall(shape, rng, work):
    """A thin wall within ONE body: beside the part, a tube whose inner and outer cylinders are a small gap apart and
    whose seams are turned (the two faces of one wall, meshed each in its own frame)."""
    return _close_fixture(shape, rng, thin=True)


# ---- CRS v5 step 10: low_quality kinds (small_entities above is the third) -------------------------------------

#: both are a share of the part's diagonal; the low_quality detector's e is a tenth of the shipped element size
#: (h = diag / 10, so e = diag / 100): both injected sizes sit well under it. The sliver depth is the one the probe
#: showed every mesher failing on (KIND_EVIDENCE); at 1e-3 of the diagonal it meshed clean
_FLOATING_EDGE_SHARE = (1e-3, 2e-3)
_SLIVER_DEPTH_SHARE = (1e-5, 2e-5)


def _floating_solid(shape, rng, work):
    """A tiny box floating clear of the part (an exporter's stray construction body): edge 0.1-0.2% of the diagonal,
    at a gap of 5% of the diagonal beyond the part's box along a random axis."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    lo, hi = list(box.Get()[:3]), list(box.Get()[3:])
    diag = _diag(shape)
    edge = diag * rng.uniform(*_FLOATING_EDGE_SHARE)
    axis = rng.randrange(3)
    corner = [0.5 * (a + b) for a, b in zip(lo, hi, strict=True)]
    corner[axis] = hi[axis] + 0.05 * diag
    tiny = BRepPrimAPI_MakeBox(gp_Pnt(*corner), edge, edge, edge).Shape()
    compound, builder = TopoDS_Compound(), BRep_Builder()
    builder.MakeCompound(compound)
    builder.Add(compound, shape)
    builder.Add(compound, tiny)
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return compound, {"entities": [{"type": "body", "body_index": 1, "role": "floating_tiny_solid",
                                    "at": [round(c, 6) for c in corner], "edge_mm": round(edge, 9)}],
                      "volume_share": round(edge ** 3 / max(abs(props.Mass()), 1e-12), 12)}


def _narrow_faces(shape: TopoDS_Shape, width: float) -> list[tuple[list[float], float]]:
    out = []
    for f in _entities(shape, TopAbs_FACE):
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(f, props)
        area = props.Mass()
        if area > 0 and (12.0 * max(min(props.PrincipalProperties().Moments()), 0.0) / area) ** 0.5 < width:
            out.append(_center_area(f))
    return out


def _sliver_face(shape, rng, work):
    """A sliver: a shallow step (0.1-0.2% of the diagonal deep) cut into one of the largest planar faces across a line
    through its middle, so the step's wall is a long face as narrow as the step is deep - the narrow region a
    lossy exporter or a mis-snapped sketch leaves. The cut is a slab +-depth about the face's plane over half the
    face's own extent, so nothing far from the face is touched."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace as _MakeFace
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakePolygon
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.TopAbs import TopAbs_REVERSED

    faces = _entities(shape, TopAbs_FACE)
    planar = sorted((i for i, f in enumerate(faces) if _is_plane(f)), key=lambda i: -_center_area(faces[i])[1])
    if not planar:
        raise InjectionNotApplicable("sliver_face needs a planar face to step")
    diag = _diag(shape)
    depth = diag * rng.uniform(*_SLIVER_DEPTH_SHARE)
    before = _narrow_faces(shape, 2.0 * depth)
    for i in planar[:3]:
        face = TopoDS.Face_s(faces[i])
        adaptor = BRepAdaptor_Surface(face)
        n = adaptor.Plane().Axis().Direction()
        if face.Orientation() == TopAbs_REVERSED:
            n.Reverse()
        u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
        um = u0 + (u1 - u0) * rng.uniform(0.35, 0.65)
        pad = 0.01 * diag
        poly = BRepBuilderAPI_MakePolygon()
        for u, v in ((um, v0 - pad), (u1 + pad, v0 - pad), (u1 + pad, v1 + pad), (um, v1 + pad)):
            p = adaptor.Value(u, v)
            poly.Add(gp_Pnt(p.X() + n.X() * depth, p.Y() + n.Y() * depth, p.Z() + n.Z() * depth))
        poly.Close()
        slab = BRepPrimAPI_MakePrism(_MakeFace(poly.Wire()).Face(),
                                     gp_Vec(-2 * depth * n.X(), -2 * depth * n.Y(), -2 * depth * n.Z())).Shape()
        cut = BRepAlgoAPI_Cut(shape, slab)
        if not cut.IsDone():
            continue
        out = cut.Shape()
        known = {tuple(c) for c, _a in before}
        slivers = [{"type": "face", "src_index": None, "center": c, "area": a}
                   for c, a in _narrow_faces(out, 2.0 * depth) if tuple(c) not in known]
        if slivers:
            return out, {"entities": slivers, "host_face": _face_ref(shape, faces, i), "depth_mm": round(depth, 9)}
    raise InjectionNotApplicable("sliver_face: no step on a large planar face produced a new narrow face")


# ---- CRS v5 step 9 (surface_crossing) ----------------------------------------------------------------------------

def _two(a: TopoDS_Shape, b: TopoDS_Shape) -> TopoDS_Shape:
    compound, builder = TopoDS_Compound(), BRep_Builder()
    builder.MakeCompound(compound)
    builder.Add(compound, a)
    builder.Add(compound, b)
    return compound


def _duplicate_faces(shape, rng, work):
    """The part cut in two across its longest extent and BOTH halves left in place: the two cut faces are the same
    face written twice (same plane, same boundary, one per body), touching and not shared - an assembly whose mating
    faces were exported separately."""
    halves, axis, at = _halves(shape, rng)
    if halves is None:
        return None, {}
    bodies = [{"type": "body", "body_index": 0, "role": "low_half"},
              {"type": "body", "body_index": 1, "role": "high_half"}]
    return _two(*halves), {"entities": bodies, "axis": axis, "cut_at": round(at, 9)}


#: true_crossing's tilt (degrees): enough that the two cut faces cross along a line well inside them, small enough that
#: the wedges between them are thin
_CROSS_TILT_DEG = (0.5, 2.0)


def _true_crossing(shape, rng, work):
    """Two faces passing through each other: the part cut in two (as duplicate_faces) and one half tilted a small angle
    about a line in the cut plane through the middle of the part, so the two cut faces cross along that line (thin
    wedges of shared volume either side of it)."""
    from OCP.gp import gp_Ax1, gp_Dir

    halves, axis, at = _halves(shape, rng)
    if halves is None:
        return None, {}
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    lo, hi = box.Get()[:3], box.Get()[3:]
    centre = [(lo[k] + hi[k]) / 2 for k in range(3)]
    centre[axis] = at
    turn = (axis + 1 + rng.randrange(2)) % 3        # a line IN the cut plane: one of the two other axes
    direction = [0.0, 0.0, 0.0]
    direction[turn] = 1.0
    tilt = rng.uniform(*_CROSS_TILT_DEG) * rng.choice([-1, 1])
    t = gp_Trsf()
    t.SetRotation(gp_Ax1(gp_Pnt(*centre), gp_Dir(*direction)), math.radians(tilt))
    tilted = BRepBuilderAPI_Transform(halves[1], t, True).Shape()
    bodies = [{"type": "body", "body_index": 0, "role": "low_half"},
              {"type": "body", "body_index": 1, "role": "high_half_tilted", "tilt_deg": round(tilt, 9),
               "about": [round(c, 9) for c in centre], "axis": turn}]
    return _two(halves[0], tilted), {"entities": bodies, "axis": axis, "cut_at": round(at, 9)}


#: edge_through_face's fin, as shares of the part's diagonal: its depth through the face (half in, half out), its
#: width along the face, and its thickness
_FIN = (0.2, 0.1, 0.01)


def _edge_through_face(shape, rng, work):
    """A thin feature pushed through a neighbouring face: a thin plate (a fin) standing on a planar face of the part,
    at a point inside the face, sunk through it - half its depth inside the part, half outside. Its edges pass through
    the face. Two bodies."""
    from OCP.BRepClass import BRepClass_FaceClassifier
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.ElSLib import ElSLib
    from OCP.gp import gp_Ax2, gp_Dir
    from OCP.TopAbs import TopAbs_IN, TopAbs_REVERSED

    faces = _entities(shape, TopAbs_FACE)
    planar = [i for i, f in enumerate(faces) if _is_plane(f)]
    rng.shuffle(planar)
    diag = _diag(shape)
    depth, width, thick = (s * diag for s in _FIN)
    for i in planar:
        face = TopoDS.Face_s(faces[i])
        centre, area = _center_area(face)
        if area < 4 * width * thick:
            continue
        plane = BRepAdaptor_Surface(face).Plane()
        p = gp_Pnt(*centre)
        u, v = ElSLib.Parameters_s(plane, p)
        if BRepClass_FaceClassifier(face, gp_Pnt2d(u, v), 1e-7).State() != TopAbs_IN:
            continue
        normal = plane.Axis().Direction()
        if face.Orientation() == TopAbs_REVERSED:
            normal.Reverse()
        x_dir = plane.XAxis().Direction()
        y_dir = normal.Crossed(x_dir)
        corner = gp_Pnt(*(c - normal.Coord(k + 1) * depth / 2 - x_dir.Coord(k + 1) * width / 2
                          - y_dir.Coord(k + 1) * thick / 2 for k, c in enumerate(centre)))
        fin = BRepPrimAPI_MakeBox(gp_Ax2(corner, gp_Dir(normal.XYZ()), gp_Dir(x_dir.XYZ())), width, thick,
                                  depth).Shape()
        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(BRepAlgoAPI_Common(shape, fin).Shape(), props)
        if props.Mass() <= 0.0:
            continue
        return _two(shape, fin), {"entities": [_face_ref(shape, faces, i), {"type": "body", "body_index": 1,
                                                                             "role": "fin"}],
                                  "fin_mm": [round(depth, 9), round(width, 9), round(thick, 9)],
                                  "overlap_volume": round(props.Mass(), 6)}
    return None, {}


def _self_intersecting_face(shape, rng, work):
    """ONE solid whose faces cross each other: the part's first solid rebuilt as a single solid holding its own outer
    shell AND a copy of that shell shifted 15-35% of the part's width along one axis - the two face sets interpenetrate
    inside one body (an exporter that merged two overlapping lumps into one solid). Layer 1's self-interference
    analysis names the body (`self_intersection`); no other body is touched."""
    from OCP.BRepClass3d import BRepClass3d
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopoDS import TopoDS_Shell, TopoDS_Solid

    solids = _entities(shape, TopAbs_SOLID)
    if not solids:
        return None, {}
    solid = solids[0]
    shell = BRepClass3d.OuterShell_s(TopoDS.Solid_s(solid))
    if shell.IsNull():
        return None, {}
    box = Bnd_Box()
    BRepBndLib.Add_s(solid, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    axis = rng.randrange(3)
    shift = [0.0, 0.0, 0.0]
    shift[axis] = [x1 - x0, y1 - y0, z1 - z0][axis] * rng.uniform(0.15, 0.35)
    t = gp_Trsf()
    t.SetTranslation(gp_Vec(*shift))
    twin = BRepBuilderAPI_Transform(_copy(shell), t, True).Shape()
    builder = BRep_Builder()
    # ONE shell holding both face sets: two shells in one solid are split back into two solids by the STEP reader on
    # 3 of 4 sources (VM probe, step 8: layer 1 then reads body_overlap, not self_intersection)
    one = TopoDS_Shell()
    builder.MakeShell(one)
    for face in (*_entities(shell, TopAbs_FACE), *_entities(twin, TopAbs_FACE)):
        builder.Add(one, face)
    merged = TopoDS_Solid()
    builder.MakeSolid(merged)
    builder.Add(merged, one)
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    builder.Add(compound, merged)
    for other in solids[1:]:
        builder.Add(compound, other)
    return compound, {"entities": [{"type": "body", "body_index": 0, "role": "two_crossing_shells",
                                    "shift": [round(c, 9) for c in shift]}]}


def _inside_out(shape, rng, work):
    """Every solid written inside out (reversed): an exporter that flipped the shell's orientation."""
    from OCP.TopAbs import TopAbs_SOLID

    solids = _entities(shape, TopAbs_SOLID)
    if not solids:
        return None, {}
    compound, builder = TopoDS_Compound(), BRep_Builder()
    builder.MakeCompound(compound)
    for s in solids:
        builder.Add(compound, s.Reversed())
    return compound, {"entities": [{"type": "body", "body_index": i, "role": "reversed"} for i in range(len(solids))]}


def _convex_planar_edge(shape, rng):
    """(edge, its start, its vector, the two faces' outward normals) for a straight edge between two planar faces of
    a convex dihedral, picked at random; None when the part has none."""
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.GeomAbs import GeomAbs_Line
    from OCP.TopAbs import TopAbs_OUT, TopAbs_REVERSED
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

    ancestors = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, ancestors)
    order = list(range(1, ancestors.Extent() + 1))
    rng.shuffle(order)
    for i in order:
        edge = TopoDS.Edge_s(ancestors.FindKey(i))
        owners = ancestors.FindFromIndex(i)
        if owners.Size() != 2 or BRepAdaptor_Curve(edge).GetType() != GeomAbs_Line:
            continue
        normals = []
        for face in (owners.First(), owners.Last()):
            surface = BRepAdaptor_Surface(TopoDS.Face_s(face))
            if surface.GetType() != GeomAbs_Plane:
                break
            n = surface.Plane().Axis().Direction()
            sign = -1.0 if face.Orientation() == TopAbs_REVERSED else 1.0
            normals.append([sign * n.X(), sign * n.Y(), sign * n.Z()])
        if len(normals) != 2 or abs(sum(a * b for a, b in zip(*normals, strict=True))) > 0.999:
            continue
        curve = BRepAdaptor_Curve(edge)
        p0, p1 = curve.Value(curve.FirstParameter()), curve.Value(curve.LastParameter())
        length = p0.Distance(p1)
        mid = [(a + b) / 2 for a, b in zip((p0.X(), p0.Y(), p0.Z()), (p1.X(), p1.Y(), p1.Z()), strict=True)]
        probe = gp_Pnt(*[m + 1e-3 * length * (a + b) for m, a, b in zip(mid, *normals, strict=True)])
        if BRepClass3d_SolidClassifier(shape, probe, 1e-7).State() != TopAbs_OUT:
            continue
        return edge, p0, [p1.X() - p0.X(), p1.Y() - p0.Y(), p1.Z() - p0.Z()], normals, length
    return None


def _non_manifold_edge(shape, rng, work):
    """A block glued to the part along ONE convex straight edge and written as ONE solid with it: that edge is then
    shared by four faces (non-manifold). An exporter that merged two bodies touching along an edge into one shell."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_BuilderAlgo
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon, BRepBuilderAPI_MakeSolid
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.TopoDS import TopoDS_Shell
    from OCP.TopTools import TopTools_ListOfShape

    found = _convex_planar_edge(shape, rng)
    if found is None:
        return None, {}
    edge, p0, d, (n1, n2), length = found
    s = min(0.5 * length, 0.05 * _diag(shape))
    corner = [p0.X(), p0.Y(), p0.Z()]
    pts = [corner, [c + s * a for c, a in zip(corner, n1, strict=True)],
           [c + s * (a + b) for c, a, b in zip(corner, n1, n2, strict=True)],
           [c + s * b for c, b in zip(corner, n2, strict=True)]]
    poly = BRepBuilderAPI_MakePolygon()
    for p in pts:
        poly.Add(gp_Pnt(*p))
    poly.Close()
    block = BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(poly.Wire(), True).Face(), gp_Vec(*d)).Shape()
    args = TopTools_ListOfShape()
    args.Append(shape)
    args.Append(block)
    glue = BRepAlgoAPI_BuilderAlgo()
    glue.SetArguments(args)
    glue.Build()
    if not glue.IsDone():
        return None, {}
    shell, builder = TopoDS_Shell(), BRep_Builder()
    builder.MakeShell(shell)
    for face in _entities(glue.Shape(), TopAbs_FACE):
        builder.Add(shell, face)
    solid = BRepBuilderAPI_MakeSolid(shell).Solid()
    return solid, {"entities": [{"type": "edge", "center": [round(c + v / 2, 9) for c, v in zip(corner, d, strict=True)],
                                 "length": round(length, 9)}], "block_mm": round(s, 9)}


def _sheet_body(shape, rng, work):
    """The part plus one of its faces copied as a free sheet, moved clear of the part: a surface body an exporter
    left in the file, never meant to be solid. No repair may close it (expected outcome: correctly declined)."""
    from OCP.TopoDS import TopoDS_Shell

    faces = _entities(shape, TopAbs_FACE)
    if not faces:
        return None, {}
    i = max(range(len(faces)), key=lambda k: _center_area(faces[k])[1])
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    t = gp_Trsf()
    t.SetTranslation(gp_Vec(0.0, 0.0, (z1 - z0) + 0.1 * _diag(shape)))
    sheet = BRepBuilderAPI_Transform(_copy(faces[i]), t, True).Shape()
    shell, builder = TopoDS_Shell(), BRep_Builder()
    builder.MakeShell(shell)
    builder.Add(shell, sheet)
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    builder.Add(compound, shape)
    builder.Add(compound, shell)
    return compound, {"entities": [{**_face_ref(shape, faces, i), "role": "copied_as_free_sheet"}]}


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


# ---- CRS v5 step 5: closed_periodic_face and invalid_brep_tolerance kinds ---------------------------------------

def _periodic_no_seam(shape, rng, ctx):
    """A full cylindrical face rebuilt WITHOUT its seam: the same surface, bounded by its two circles alone (the
    closed periodic face an exporter that does not split at the seam writes). The circles get the pcurves a seamless
    face needs (v = const lines across one period); the seam edge is gone from the shell."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeWire
    from OCP.GeomAbs import GeomAbs_Cylinder

    faces = _entities(shape, TopAbs_FACE)
    seamed = [i for i, f in enumerate(faces)
              if _has_seam(f) and BRepAdaptor_Surface(TopoDS.Face_s(f)).GetType() == GeomAbs_Cylinder]
    if not seamed:
        raise InjectionNotApplicable("periodic_no_seam needs a cylindrical face with a seam")
    i = rng.choice(seamed)
    face = TopoDS.Face_s(faces[i])
    loops = [TopoDS.Edge_s(e) for e in _entities(face, TopAbs_EDGE)
             if not BRep_Tool.IsClosed_s(TopoDS.Edge_s(e), face) and not BRep_Tool.Degenerated_s(TopoDS.Edge_s(e))]
    if len(loops) != 2 or not all(BRep_Tool.IsClosed_s(e) for e in loops):
        raise InjectionNotApplicable("periodic_no_seam needs a full cylinder bounded by two closed circles")
    surface = BRep_Tool.Surface_s(face)
    maker = BRepBuilderAPI_MakeFace(surface, BRepBuilderAPI_MakeWire(loops[0]).Wire(), True)
    maker.Add(BRepBuilderAPI_MakeWire(loops[1]).Wire())
    if not maker.IsDone():
        raise InjectionNotApplicable("periodic_no_seam: the seamless face could not be built")
    reshape = BRepTools_ReShape()
    reshape.Replace(face, maker.Face())
    out = reshape.Apply(shape)
    return out, {"entities": [_face_ref(shape, faces, i)], "variant": "seam_removed"}


_HUGE_MM = 5e9


def _unbounded_plane(shape, rng, ctx):
    """A construction plane an exporter left in the body list, bounded at +-`_HUGE_MM` (5e9 mm): beyond layer 1's
    unbounded line (`units.UNBOUNDED_DIAGONAL_MM`, 1e9) and `trim_unbounded`'s per-face test. A truly infinite face
    (no wire) is NOT used: the STEP writer drops it, so the file read back clean (measured on jig, v5 step 5)."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.gp import gp_Dir, gp_Pln

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    axis = rng.randrange(3)
    normal = [0.0, 0.0, 0.0]
    normal[axis] = 1.0
    at = [x1, y1, z1][axis] + 0.5 * math.dist((x0, y0, z0), (x1, y1, z1))
    origin = [0.0, 0.0, 0.0]
    origin[axis] = at
    plane = BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(*origin), gp_Dir(*normal)), -_HUGE_MM, _HUGE_MM, -_HUGE_MM,
                                    _HUGE_MM).Face()
    compound, builder = TopoDS_Compound(), BRep_Builder()
    builder.MakeCompound(compound)
    builder.Add(compound, shape)
    builder.Add(compound, plane)
    return compound, {"entities": [{"type": "face", "src_index": None, "role": "infinite_plane", "normal": normal,
                                    "at": round(at, 6), "half_size_mm": _HUGE_MM}]}


def _pole_degenerate(shape, rng, ctx):
    """A real (non-zero-length) edge flagged DEGENERATED: the face then claims a pole where its surface is not
    collapsed, the fault `fix_poles` (ShapeFix_Face FixDegenerated) answers. The shortest such edge is picked, so the
    claim is as nearly plausible as the part allows."""
    edges = _entities(shape, TopAbs_EDGE)
    faces = _entities(shape, TopAbs_FACE)
    lengths = []
    for j, e in enumerate(edges):
        edge = TopoDS.Edge_s(e)
        if BRep_Tool.Degenerated_s(edge) or BRep_Tool.IsClosed_s(edge):
            continue
        props = GProp_GProps()
        BRepGProp.LinearProperties_s(edge, props)
        lengths.append((props.Mass(), j))
    if not lengths:
        raise InjectionNotApplicable("pole_degenerate needs an open, non-degenerated edge")
    lengths.sort()
    _length, j = lengths[min(len(lengths) - 1, rng.randrange(3))]
    out = _copy(shape)
    edge = TopoDS.Edge_s(_entities(out, TopAbs_EDGE)[j])
    BRep_Builder().Degenerated(edge, True)
    return out, {"entities": [{**_edge_ref(edges, j), "length": round(_length, 9)}], "faces": len(faces)}


def _units_scale(shape, rng, ctx):
    """The geometry written in METRES under the file's millimetre header (x0.001): a units mistake the owner's rule
    must catch (`repair_v5.brep_faults.units_verdict`). The header is untouched; only the numbers are wrong."""
    t = gp_Trsf()
    t.SetScale(gp_Pnt(0.0, 0.0, 0.0), 0.001)
    out = BRepBuilderAPI_Transform(shape, t, True).Shape()
    return out, {"entities": [{"type": "part", "src_index": None, "factor": 0.001}], "scope": "whole_file",
                 "diag_before": round(_diag(shape), 6), "diag_after": round(_diag(out), 9)}


def _bad_spline(shape, rng, ctx):
    """One curved (or, failing that, planar) face's carrier replaced by a B-spline through the SAME surface sampled
    at cubically crowded parameters but fitted ISO-PARAMETRICALLY: nearly the same geometry, a parameter speed that
    varies ~27x along each iso (the badly parameterised spline of a careless approximation). The edges keep their
    curves; the STEP reader re-projects the pcurves, so what survives is the parameterisation."""
    from OCP.Approx import Approx_IsoParametric

    faces = _entities(shape, TopAbs_FACE)
    curved = [i for i, f in enumerate(faces) if not _is_plane(f) and not _has_seam(f)]
    pool = curved or [i for i, f in enumerate(faces) if not _has_seam(f)]
    if not pool:
        raise InjectionNotApplicable("bad_spline needs a face without a seam")
    i = rng.choice(pool)
    out = _copy(shape)
    face = TopoDS.Face_s(_entities(out, TopAbs_FACE)[i])
    surf = BRep_Tool.Surface_s(face)
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    n = 12
    pts = TColgp_Array2OfPnt(1, n, 1, n)
    for a in range(n):
        for b in range(n):
            s_, t_ = (a / (n - 1)) ** 3, (b / (n - 1)) ** 3          # crowded at the low end of both parameters
            pts.SetValue(a + 1, b + 1, surf.Value(u0 + (u1 - u0) * s_, v0 + (v1 - v0) * t_))
    fitter = GeomAPI_PointsToBSplineSurface()
    fitter.Init(pts, Approx_IsoParametric, 3, 3, GeomAbs_C2, 1e-3)
    fit = fitter.Surface()
    pcurves = []
    for edge in _entities(face, TopAbs_EDGE):
        pc = BRep_Tool.CurveOnSurface_s(TopoDS.Edge_s(edge), face, 0.0, 0.0)
        if pc is not None:
            pcurves.append((TopoDS.Edge_s(edge), pc))
    builder = BRep_Builder()
    builder.UpdateFace(face, fit, TopLoc_Location(), BRep_Tool.Tolerance_s(face))
    _fit_to_uv_box(fit, u0, u1, v0, v1)
    for edge, pc in pcurves:
        builder.UpdateEdge(edge, pc, fit, TopLoc_Location(), BRep_Tool.Tolerance_s(edge))
    return out, {"entities": [_face_ref(shape, faces, i)], "grid": n, "crowding": "cubic"}


#: the lens's half-angle (degrees): a circular segment this thin meets its chord at a knife edge, and a mesher that
#: puts one segment on the arc lays the arc face's facets on the chord face's (facets crossing ACROSS a shared edge)
_LENS_HALF_DEG = (8.0, 14.0)


def _knife_lens(shape, rng, work):
    """Beside the part (one more body), a thin circular segment: a cylinder of radius 5% of the diagonal cut by a chord
    plane at `_LENS_HALF_DEG` from its rim. Its arc face and its chord face share two knife edges (the
    facet_crossing_local class's adjacent pair; nothing apart, nothing overlapping)."""
    import math as _m

    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    r = _CLOSE_RADIUS_OF_DIAG * _diag(shape)
    half = _m.radians(rng.uniform(*_LENS_HALF_DEG))
    base, h = _beside(shape, r)
    chord = r * _m.cos(half)
    keep = BRepPrimAPI_MakeBox(gp_Pnt(base[0] + chord, base[1] - r, base[2] - h),
                               gp_Pnt(base[0] + 2.0 * r, base[1] + r, base[2] + 2.0 * h)).Shape()
    lens = BRepAlgoAPI_Common(_cylinder(base, r, h, 0.0), keep).Shape()
    compound, builder = TopoDS_Compound(), BRep_Builder()
    builder.MakeCompound(compound)
    builder.Add(compound, shape)
    builder.Add(compound, lens)
    return compound, {"entities": [{"type": "body", "body_index": 0, "role": "original"},
                                   {"type": "body", "body_index": 1, "role": "knife_lens", "radius": round(r, 9),
                                    "half_angle_deg": round(_m.degrees(half), 6),
                                    "thickness_mm": round(r - chord, 9)}]}


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
    "periodic_no_seam": _periodic_no_seam,
    "unbounded_plane": _unbounded_plane,
    "pole_degenerate": _pole_degenerate,
    "units_scale": _units_scale,
    "bad_spline": _bad_spline,
    "misalignment": _misalignment,
    "duplicate_body": _duplicate_body,
    "shaft_in_bore": _shaft_in_bore,
    "thin_wall": _thin_wall,
    "floating_solid": _floating_solid,
    "sliver_face": _sliver_face,
    "duplicate_faces": _duplicate_faces,
    "true_crossing": _true_crossing,
    "edge_through_face": _edge_through_face,
    "self_intersecting_face": _self_intersecting_face,
    "inside_out": _inside_out,
    "non_manifold_edge": _non_manifold_edge,
    "sheet_body": _sheet_body,
    "knife_lens": _knife_lens,
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
