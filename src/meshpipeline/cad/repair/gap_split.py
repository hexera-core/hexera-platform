# Responsibility: Resolve a SUB-MESH-SIZE GAP between two faces of one body - two surfaces that do
#                 not touch, but sit closer together than the default surface mesh's chord sag -
#                 by refining the faces' TOPOLOGY so gmsh's default triangles follow the curvature.
# Owns: the `resolve_sub_mesh_gap` operation, its split plan, and its declines.
# Boundaries: an operation like any other (operations.py's contract): it returns a NEW shape, never
#             mutates the one it was given, does not re-inspect, and never decides whether its own
#             result is kept. It moves no surface ON PURPOSE and declares what it measured anyway.
# Collaborates with: cad/repair/operations.py (the catalogue, the splice, the naming helpers),
#                    cad/repair/mesh_probe.py (the probe's mesh size and the clash evidence this
#                    operation answers), cad/repair/conservative.py (the area/volume measure the
#                    caps use, so the declaration and the cap read identical numbers).
from __future__ import annotations

import json
import logging
import math
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from meshpipeline.cad.repair.contracts import DefectCode
from meshpipeline.cad.repair.identity import EntityRef

if TYPE_CHECKING:
    from meshpipeline.cad.repair.operations import OpOutcome

logger = logging.getLogger(__name__)

# WHAT THE DEFECT IS, MEASURED (lane F/G, wave 3, 2026-11-20).
#
# Sixteen of the 120 real failing files fail gmsh's default probe with an edge through a face or
# overlapping facets, and the two faces whose triangles cross DO NOT TOUCH in the B-rep: the
# distance between them is 0.2%-21% of the probe's mesh size (diagonal / 40). Almost every pair is
# a DESIGNED thin wall - coaxial cylinders, cones, tori and spheres at round offsets (1 mm, 2.5 mm,
# 4 mm, 25.4 mm = 1 in, 0.0508 mm = 0.002 in). A triangle of size h on a surface of radius R sags
# h^2 / (8 R) inside it; when that sag is wider than the wall, the outer surface's triangles pass
# through the inner surface and the volume mesh is refused.
#
# WHY NOT CLOSE THE GAP. Snapping, sewing or fuzzy-fusing the two faces together deletes the wall
# - on these parts that is the design, not an artefact. Lane G's ruling stands: no operation
# honestly closes a sub-chord gap. What CAN be done in the CAD, without moving the surface, is to
# give gmsh vertices where it needs them: split the faces along their own iso-parametric curves so
# each patch is narrow enough that a chord across it sags less than half the gap. gmsh meshes every
# boundary edge, so each patch boundary is a line of mesh vertices ON the surface.
#
# WHAT IT COSTS, STATED: more faces (bounded by MAX_ISO_EDGES) and a mesh that is finer on those
# faces. The surface, every vertex and every existing edge's curve are unchanged; the only edges
# that change are the boundary edges an iso-curve ends on, which are split at a point on their own
# curve. The area and volume measured afterwards differ from before only by the kernel's
# integration noise, and that measured difference is DECLARED, rounded away from zero.

#: The share of the gap a chord's sag may use. A quarter leaves the rest for the facing surface's
#: own sag, for gmsh's freedom to place interior vertices, and for the neighbours' unsplit edges
#: (measured on 0056_00565386: at a half, the clash moved onto the new patches).
SAG_SHARE = 0.25
#: No patch or new edge may be one the localiser would report (localize.SMALL_FACE_AREA_FRACTION,
#: SMALL_EDGE_LENGTH_FRACTION): the session reads a new warning as a new defect and rolls the split
#: back (measured on 0051_00519054). This margin keeps every patch and span above twice the bar.
SMALL_MARGIN = 2.0
#: At most this many patches per face per parametric direction.
MAX_SEGMENTS = 64
#: At most this many iso-curves in one application, across every face it splits. Above it the part
#: would be re-partitioned rather than refined, and the operation declines `too_fine`.
MAX_ISO_EDGES = 1200
#: The nearest faces (by B-rep distance) of the target's own body below the mesh size that are
#: split with it - the other side of the wall.
MAX_PARTNERS = 3
#: A partner is the OTHER SIDE OF THE SAME WALL: within this factor of the narrowest gap. Any face
#: merely closer than the mesh size is not one - measured on 0060_00602471, splitting a third face
#: at 0.8 mm left a new overlap on one of its patches.
PARTNER_GAP_FACTOR = 1.5
#: An iso-curve is dropped when it would land within this share of a span of one of the face's own
#: vertices: it would leave a boundary edge far shorter than the rest (the localiser's small-edge
#: warning, measured on 0060_00602471).
VERTEX_CLEARANCE = 0.25
#: How many of the body's faces, nearest by bounding box, are measured for the partner search.
MAX_CANDIDATES = 48
#: A split moves no surface; anything beyond kernel integration noise is a split that went wrong.
#: Measured noise on the 16 gap files: volume <= 6e-5 of the part, area <= 2.4e-5.
MAX_VOLUME_NOISE_RATIO = 1e-3
MAX_AREA_NOISE_RATIO = 1e-4

NAME = "resolve_sub_mesh_gap"


def _mesh_size(diagonal_mm: float) -> float:
    from meshpipeline.cad.repair import mesh_probe

    return float(diagonal_mm) / float(mesh_probe._SIZE_DIVISOR)


def _rounded_up(value: float, places: int = 6) -> float:
    """`value` rounded AWAY FROM ZERO at `places` (investigator_tools' convention, #496): a claim
    of the reported figure is never under the real movement."""
    if not math.isfinite(value):
        return value
    scaled = value * 10 ** places
    whole = math.floor(abs(scaled))
    if abs(scaled) > whole:
        whole += 1
    return math.copysign(whole / 10 ** places, value)


def _bbox(shape):
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    return box


def _distance(a, b) -> float | None:
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    try:
        extrema = BRepExtrema_DistShapeShape(a, b)
        return float(extrema.Value()) if extrema.IsDone() else None
    except Exception as exc:  # noqa: BLE001 - an unmeasured pair is never a partner
        logger.debug("gap_split: distance failed (%s)", exc)
        return None


def _touch_mm(a, b) -> float:
    """How close two faces may be and still TOUCH: the larger of their own B-rep tolerances. NOT
    the run's healing tolerance, which is far coarser (0.1 mm by default) and would read the
    corpus's 0.0508 mm walls as faces that meet."""
    from OCP.BRep import BRep_Tool
    from OCP.TopoDS import TopoDS

    return max(float(BRep_Tool.Tolerance_s(TopoDS.Face_s(a))),
               float(BRep_Tool.Tolerance_s(TopoDS.Face_s(b))))


def partners(owner, face, *, mesh_size: float) -> list[tuple[object, float]]:
    """The faces of `owner` across a SUB-MESH GAP from `face`: B-rep distance above both faces' own
    tolerance (they do not meet) and below the mesh size, nearest first, at most MAX_PARTNERS."""
    from OCP.TopAbs import TopAbs_FACE

    from meshpipeline.cad.repair.localize import _indexed

    target_box = _bbox(face)
    faces = _indexed(owner, TopAbs_FACE)
    near: list[tuple[float, object]] = []
    for index in range(1, faces.Extent() + 1):
        other = faces.FindKey(index)
        if other.IsSame(face):
            continue
        gap = target_box.Distance(_bbox(other))
        if gap < mesh_size:
            near.append((gap, other))
    near.sort(key=lambda item: item[0])
    found: list[tuple[object, float]] = []
    for _box_gap, other in near[:MAX_CANDIDATES]:
        distance = _distance(face, other)
        if distance is not None and _touch_mm(face, other) < distance < mesh_size:
            found.append((other, distance))
    found.sort(key=lambda item: item[1])
    if not found:
        return []
    narrowest = found[0][1]
    return [item for item in found if item[1] <= PARTNER_GAP_FACTOR * narrowest][:MAX_PARTNERS]


def _directional_curvature(props, which: str) -> tuple[float, float]:
    """(|normal curvature| along the u or v iso direction, |d/du or d/dv|) at a sampled point."""
    if not props.IsNormalDefined():
        return 0.0, 0.0
    normal = props.Normal()
    first = props.D1U() if which == "u" else props.D1V()
    second = props.D2U() if which == "u" else props.D2V()
    speed = first.Magnitude()
    if speed < 1e-12:
        return 0.0, 0.0
    along = second.X() * normal.X() + second.Y() * normal.Y() + second.Z() * normal.Z()
    return abs(along) / (speed * speed), speed


def split_plan(face, *, gap_mm: float, mesh_size: float, part_area_mm2: float = 0.0,
               diagonal_mm: float = 0.0) -> dict[str, list[float]]:
    """The parameter values to split `face` at, per direction.

    For each direction the largest normal curvature k and the longest iso-length L are sampled
    (5 x 9 points). A chord c sags c^2 k / 8, so c = sqrt(8 * SAG_SHARE * gap / k) keeps the sag
    under SAG_SHARE of the gap; c never exceeds the mesh size (gmsh already gets that), and a flat
    direction (k ~ 0) is never split. The face is divided into ceil(L / c) equal parameter spans,
    at most MAX_SEGMENTS, and never so finely that a span is a small edge or a patch a small face
    by the localiser's own bars (SMALL_MARGIN x them)."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepTools import BRepTools
    from OCP.GeomLProp import GeomLProp_SLProps
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad.repair.localize import (
        SMALL_EDGE_LENGTH_FRACTION,
        SMALL_FACE_AREA_FRACTION,
        _face_area,
    )

    face = TopoDS.Face_s(face)
    surface = BRep_Tool.Surface_s(face)
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    plan: dict[str, list[float]] = {"u": [], "v": []}
    counts: dict[str, int] = {}
    lengths: dict[str, float] = {}
    for which, (a0, a1), (b0, b1) in (("u", (u0, u1), (v0, v1)), ("v", (v0, v1), (u0, u1))):
        if not (a1 > a0 and b1 >= b0):
            continue
        kmax = 0.0
        length = 0.0
        for j in range(5):
            b = b0 + (b1 - b0) * (j + 0.5) / 5
            run = 0.0
            for i in range(9):
                a = a0 + (a1 - a0) * (i + 0.5) / 9
                u, v = (a, b) if which == "u" else (b, a)
                props = GeomLProp_SLProps(surface, u, v, 2, 1e-9)
                k, speed = _directional_curvature(props, which)
                kmax = max(kmax, k)
                run += speed * (a1 - a0) / 9
            length = max(length, run)
        if kmax * mesh_size < 1e-6 or length <= 0:
            continue                      # flat in this direction: no sag to resolve
        chord = min(mesh_size, math.sqrt(8.0 * SAG_SHARE * max(gap_mm, 1e-9) / kmax))
        segments = min(MAX_SEGMENTS, math.ceil(length / chord))
        if diagonal_mm > 0:
            shortest = SMALL_MARGIN * SMALL_EDGE_LENGTH_FRACTION * diagonal_mm
            segments = min(segments, max(1, int(length / shortest)))
        counts[which], lengths[which] = segments, length
    if part_area_mm2 > 0 and counts:
        # EVERY PATCH ABOVE THE SLIVER BAR: shrink the finer direction until the face's own area
        # over the patch count clears it.
        smallest = SMALL_MARGIN * SMALL_FACE_AREA_FRACTION * part_area_mm2
        area = float(_face_area(face))
        while counts and area / math.prod(counts.values()) < smallest and max(counts.values()) > 1:
            finest = max(counts, key=lambda k: counts[k])
            counts[finest] -= 1
    for which, segments in counts.items():
        if segments <= 1:
            continue
        a0, a1 = (u0, u1) if which == "u" else (v0, v1)
        span = (a1 - a0) / segments
        taken = _vertex_parameters(face, which)
        plan[which] = [value for value in (a0 + span * i for i in range(1, segments))
                       if all(abs(value - t) >= VERTEX_CLEARANCE * span for t in taken)]
    return plan


def _vertex_parameters(face, which: str) -> list[float]:
    """The u (or v) parameter of every vertex of `face`, on the face."""
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_VERTEX
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    out: list[float] = []
    explorer = TopExp_Explorer(face, TopAbs_VERTEX)
    while explorer.More():
        try:
            uv = BRep_Tool.Parameters_s(TopoDS.Vertex_s(explorer.Current()), face)
            out.append(float(uv.X() if which == "u" else uv.Y()))
        except Exception as exc:  # noqa: BLE001 - a vertex the face cannot place is skipped
            logger.debug("gap_split: vertex parameter (%s)", exc)
        explorer.Next()
    return out


def iso_edges(face, plan: dict[str, list[float]]) -> list:
    """The plan's iso-curves, each TRIMMED TO THE FACE (`BRepAlgoAPI_Common`), so no split edge
    runs on into a neighbour that happens to share the surface."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    face = TopoDS.Face_s(face)
    surface = BRep_Tool.Surface_s(face)
    u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
    raw = [BRepBuilderAPI_MakeEdge(surface.UIso(u), v0, v1).Edge() for u in plan.get("u", ())]
    raw += [BRepBuilderAPI_MakeEdge(surface.VIso(v), u0, u1).Edge() for v in plan.get("v", ())]
    trimmed: list = []
    for edge in raw:
        common = BRepAlgoAPI_Common(edge, face)
        if not common.IsDone():
            continue
        explorer = TopExp_Explorer(common.Shape(), TopAbs_EDGE)
        while explorer.More():
            trimmed.append(explorer.Current())
            explorer.Next()
    return trimmed


def _split_body(owner, edges):
    """`owner` split by `edges` lying on its faces (`BRepAlgoAPI_Splitter`): the general fuse
    splits every shared boundary edge once, so both faces sharing it keep sharing it. Returns
    (result, history) or raises."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Splitter
    from OCP.TopTools import TopTools_ListOfShape

    arguments = TopTools_ListOfShape()
    arguments.Append(owner)
    tools = TopTools_ListOfShape()
    for edge in edges:
        tools.Append(edge)
    splitter = BRepAlgoAPI_Splitter()
    splitter.SetArguments(arguments)
    splitter.SetTools(tools)
    # Deterministic: identical runs must produce identical shapes, and so identical reports.
    splitter.SetRunParallel(False)
    splitter.Build()
    if not splitter.IsDone():
        raise RuntimeError(f"{NAME}: the splitter reported errors")
    return splitter.Shape(), splitter


def _only_solid(shape):
    from OCP.TopAbs import TopAbs_SOLID

    from meshpipeline.cad.repair.localize import _indexed

    solids = _indexed(shape, TopAbs_SOLID)
    return solids.FindKey(1) if solids.Extent() == 1 else None


class ResolveSubMeshGap:
    """Split the two sides of a SUB-MESH-SIZE GAP along their own iso-curves, so the default
    surface mesh resolves the wall instead of passing through it. See the module comment for the
    measured defect and why the gap itself is never closed.

    WHAT IT ANSWERS: an edge through a face or overlapping facets on a located face, when the
    evidence says the clash is a gap - `clash_gap_mm` above zero (mesh_probe's clash locator) - or
    says nothing about a gap (a gmsh-named face; the operation measures the gap itself). A clash
    measured at 0 is two faces that really meet: a real intersection, not this operation's.

    WHAT IT MAY TOUCH: the target face's OWNING SOLID. The target and up to MAX_PARTNERS faces of
    the same solid across a gap narrower than the mesh size are split; the boundary edges the
    iso-curves end on are split at a point on their own curve, in both faces sharing them (the
    general fuse keeps the body conformal); the solid replaces its original whole. Every other body
    is untouched.

    WHAT IT DECLARES, ALL MEASURED: `gap_mm` (the narrowest B-rep distance to a partner),
    `mesh_size_mm`, `faces_split`, `iso_edges`, `faces_added`, and AREA_DELTA / VOLUME_DELTA as
    the WHOLE PART's area and volume after minus before - measured exactly as `caps.measure`
    measures, rounded away from zero. `added` names the new patches; `rebuilt` names every face of
    the rebuilt solid.

    NEVER AUTO-ACCEPTED (review_tier.REVIEWER_ONLY_OPS): it re-partitions the customer's faces to
    suit a mesher, which is a reviewer's call however small the measured change.

    WHY EACH DECLINE EXISTS, in the order checked:
      resolving_disabled - CAD_REPAIR_RESOLVE_SUB_MESH_GAP, the consent, is absent.
      no face was named / one_face_only / not_resolved - nothing (or too much) to aim at.
      no_owner / non_manifold - no solid owns the face, or more than one does.
      no_sub_mesh_gap   - no face of the same solid sits apart from the target (beyond both
                          faces' own B-rep tolerance) yet within the mesh size: the clash is not
                          a gap this operation can see.
      flat              - neither the target nor any partner is curved enough to sag across the
                          gap: splitting would add faces and resolve nothing.
      too_fine          - the plan needs more than MAX_ISO_EDGES iso-curves.
      split_failed      - the splitter raised, or left anything but one solid.
      invalidated       - the solid was valid before and is not after.
      geometry_moved    - the measured area or volume moved beyond integration noise: a split
                          that moved surface is not this operation.
    """

    name = NAME
    answers = frozenset({DefectCode.mesh_edge_through_face, DefectCode.mesh_overlapping_facets})
    #: One solid, refined but not reshaped: wider than a one-face fix, narrower than a fuse.
    scope = 6
    whole_shape = False

    def targets_for(self, defect) -> tuple[EntityRef, ...]:
        import meshpipeline.settings.cad_repair as rcfg

        if not rcfg.CAD_REPAIR_RESOLVE_SUB_MESH_GAP:
            return ()
        if getattr(defect, "code", None) not in self.answers:
            return ()
        if defect.entity_type != "face" or not getattr(defect, "fingerprint", ""):
            return ()
        measured = (getattr(defect, "measurements", None) or {}).get("clash_gap_mm")
        if measured is not None:
            try:
                if not float(measured) > 0.0:
                    return ()             # the faces meet: a real intersection
            except (TypeError, ValueError):
                return ()
        return (EntityRef(entity_type="face", fingerprint=defect.fingerprint,
                          entity_index=int(defect.entity_index),
                          ambiguous=bool(getattr(defect, "ambiguous", False))),)

    def apply(self, shape, targets: tuple[EntityRef, ...], *,
              tolerance_mm: float) -> OpOutcome:
        import meshpipeline.settings.cad_repair as rcfg
        from meshpipeline.cad.repair.conservative import _area_and_volume
        from meshpipeline.cad.repair.identity import EntityMapping
        from meshpipeline.cad.repair.operations import (
            AREA_DELTA,
            VOLUME_DELTA,
            OpOutcome,
            _diagonal_of,
            _face_count,
            _face_names,
            _owners_of,
            _split_fingerprints,
            splice,
            sub_shape,
        )

        def declined(reason: str, **measured) -> OpOutcome:
            # NO DECLARATION ON A DECLINE: the shape is unchanged (SplitClosedFaces' rule).
            return OpOutcome(shape=shape, evidence={"declined": reason,
                                                    "faces_targeted": len(targets), **measured})

        if not rcfg.CAD_REPAIR_RESOLVE_SUB_MESH_GAP:
            return declined("resolving_disabled (CAD_REPAIR_RESOLVE_SUB_MESH_GAP)")
        if not targets:
            return declined("no face was named")
        if len(targets) > 1:
            return declined(f"one_face_only ({len(targets)} targets named)")
        ref = targets[0]
        face = sub_shape(shape, "face", ref.entity_index) if ref.entity_type == "face" else None
        if face is None:
            return declined("not_resolved (the target face is not in this shape)")
        owners = [o for o in _owners_of(shape, face) if _is_solid(o)]
        if not owners:
            return declined("no_owner (the face belongs to no solid)")
        if len(owners) > 1:
            return declined(f"non_manifold ({len(owners)} solids share the face)")
        owner = owners[0]
        diagonal = _diagonal_of(shape)
        part_area = float(_area_and_volume(shape)["surface_area_mm2"])

        # EVERY KERNEL ALGORITHM RUNS IN A BOUNDED FRESH INTERPRETER (`_bounded`): the distances,
        # the trims, the split and the guard. In the parent, BRepExtrema held the GIL for an hour
        # on wave 6 (0054_00540003) and no clock could interrupt it.
        answer, divided = _bounded(owner, face, diagonal_mm=diagonal, part_area_mm2=part_area,
                                   budget_s=KERNEL_BUDGET_S)
        measured = {k: answer[k] for k in _MEASURED_KEYS if k in answer}
        if answer.get("declined") or divided is None:
            return declined(str(answer.get("declined") or "split_failed (no result came back)"),
                            **measured)
        added = _face_count(divided) - _face_count(owner)
        if added <= 0:
            return declined("split_failed (no face was split)", **measured)

        spliced, _ = splice(shape, [(owner, divided)])
        before = _area_and_volume(shape)
        after = _area_and_volume(spliced)
        d_area = float(after["surface_area_mm2"]) - float(before["surface_area_mm2"])
        d_volume = float(after["volume_mm3"]) - float(before["volume_mm3"])
        # NO KERNEL HISTORY CROSSES THE PROCESS BOUNDARY, so the mapping is the session's
        # geometric one (box comparison), said as such rather than claimed as a kernel fact.
        return OpOutcome(shape=spliced, mapping=EntityMapping.empty(),
                         added=_split_fingerprints(owner, divided, diagonal_mm=diagonal),
                         rebuilt=_face_names(divided, diagonal_mm=diagonal),
                         evidence={**measured, "faces_added": int(added),
                                   AREA_DELTA: _rounded_up(d_area),
                                   VOLUME_DELTA: _rounded_up(d_volume),
                                   "faces_targeted": 1, "precision_mm": float(tolerance_mm)})


#: The wall-clock budget, in seconds, for the bounded kernel half of one application. Past it the
#: child is killed as a process group and the operation DECLINES `kernel_timeout` - never treats an
#: unmeasured distance as a partner, nor a half-finished split as a result.
KERNEL_BUDGET_S = 90.0

#: The child, as a list so a test can replace it. `-W ignore` keeps warnings off the JSON line.
_CHILD_CMD = [sys.executable, "-W", "ignore", "-m", "meshpipeline.cad.repair.gap_split"]

#: The evidence keys the child measures and the parent passes through.
_MEASURED_KEYS = ("gap_mm", "mesh_size_mm", "gap_ratio", "partners", "faces_split", "iso_edges",
                  "area_delta_measured_mm2", "volume_delta_measured_mm3")


def write_brep(shape, path: Path) -> None:
    from OCP.BRepTools import BRepTools

    if not BRepTools.Write_s(shape, str(path)):
        raise OSError(f"could not write {path}")


def read_brep(path: Path):
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape

    shape, builder = TopoDS_Shape(), BRep_Builder()
    if not BRepTools.Read_s(shape, str(path), builder) or shape.IsNull():
        raise OSError(f"could not read {path}")
    return shape


def _face_index(owner, face) -> int:
    from OCP.TopAbs import TopAbs_FACE

    from meshpipeline.cad.repair.localize import _indexed

    faces = _indexed(owner, TopAbs_FACE)
    return next((i for i in range(1, faces.Extent() + 1) if faces.FindKey(i).IsSame(face)), 0)


def _box_of(shape) -> list[float]:
    return [round(float(v), 6) for v in _bbox(shape).Get()]


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


def _bounded(owner, face, *, diagonal_mm: float, part_area_mm2: float,
             budget_s: float) -> tuple[dict, object | None]:
    """Run `_work` on `owner` in a FRESH INTERPRETER (never a fork: a forked child inherits the
    parent's OCC state and locks), in its own session, killed as a group past `budget_s`.

    Returns `(answer, divided solid or None)`. NEVER raises: every failure is an answer with a
    `declined` reason, and a timeout is `kernel_timeout`."""
    index = _face_index(owner, face)
    if not index:
        return {"declined": "not_resolved (the target face is not in its owner)"}, None
    try:
        with tempfile.TemporaryDirectory(prefix="gap-split-") as tmp:
            source, result = Path(tmp) / "owner.brep", Path(tmp) / "divided.brep"
            write_brep(owner, source)
            request = json.dumps({"face": index, "box": _box_of(face), "diagonal_mm": diagonal_mm,
                                  "part_area_mm2": part_area_mm2})
            proc = subprocess.Popen([*_CHILD_CMD, str(source), request, str(result)],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, start_new_session=True, text=True)
            try:
                out, _ = proc.communicate(timeout=max(float(budget_s), 0.0))
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                try:
                    proc.communicate(timeout=5)
                except Exception:  # noqa: BLE001 - the group is already gone
                    pass
                return {"declined": f"kernel_timeout (the split did not finish in "
                                    f"{float(budget_s):g} s)"}, None
            finally:
                if proc.poll() is None:
                    _kill_group(proc)
            lines = [line for line in (out or "").splitlines() if line.strip()]
            if proc.returncode != 0 or not lines:
                return {"declined": f"split_failed (the kernel child exited "
                                    f"{proc.returncode} without an answer)"}, None
            answer = json.loads(lines[-1])
            if not isinstance(answer, dict):
                return {"declined": "split_failed (the kernel child's answer was not an object)"}, None
            if answer.get("declined"):
                return answer, None
            return answer, _only_solid(read_brep(result))
    except Exception as exc:  # noqa: BLE001 - an operation declines, it never crashes the session
        return {"declined": f"split_failed ({type(exc).__name__})"}, None


def _work(owner, face, *, diagonal_mm: float, part_area_mm2: float) -> tuple[dict, object | None]:
    """The kernel half of the operation. ONLY EVER RUN IN THE CHILD (`_bounded`)."""
    size = _mesh_size(diagonal_mm)
    across = partners(owner, face, mesh_size=size)
    if not across:
        return {"declined": f"no_sub_mesh_gap (no face of the solid lies apart from the target "
                            f"but within the {size:.4g} mm mesh size)",
                "mesh_size_mm": round(size, 6)}, None
    gap = min(distance for _, distance in across)
    measured = {"gap_mm": round(gap, 6), "mesh_size_mm": round(size, 6),
                "gap_ratio": round(gap / size, 6), "partners": len(across)}
    edges: list = []
    split: list = []
    for each in (face, *(other for other, _ in across)):
        plan = split_plan(each, gap_mm=gap, mesh_size=size, part_area_mm2=part_area_mm2,
                          diagonal_mm=diagonal_mm)
        if not (plan["u"] or plan["v"]):
            continue
        pieces = iso_edges(each, plan)
        if pieces:
            edges.extend(pieces)
            split.append(each)
        if len(edges) > MAX_ISO_EDGES:
            return {"declined": f"too_fine (more than {MAX_ISO_EDGES} iso-curves needed)",
                    **measured}, None
    if not edges:
        return {"declined": "flat (no face of the gap is curved enough to sag across it)",
                **measured}, None
    measured.update(faces_split=len(split), iso_edges=len(edges))
    try:
        result, _splitter = _split_body(owner, edges)
    except Exception as exc:  # noqa: BLE001 - a kernel refusal is a decline, not a crash
        return {"declined": f"split_failed ({type(exc).__name__})", **measured}, None
    divided = _only_solid(result)
    if divided is None:
        return {"declined": "split_failed (the splitter left other than one solid)",
                **measured}, None
    from meshpipeline.cad.repair.operations import _is_valid

    if _is_valid(owner) and not _is_valid(divided):
        return {"declined": "invalidated (the split solid is not BRepCheck-valid)",
                **measured}, None
    # THE GUARD IS JUDGED AT ADAPTIVE PRECISION, on the owner alone: the default integration
    # moved a 2.8e7 mm3 part by 204 mm3 on a split that moved nothing (0056_00563150). The
    # DECLARATION is the parent's, at the caps' own default precision, so it is what they measure.
    owner_area, owner_volume = _precise(owner)
    split_area, split_volume = _precise(divided)
    d_area, d_volume = split_area - owner_area, split_volume - owner_volume
    if (abs(d_volume) > MAX_VOLUME_NOISE_RATIO * max(owner_volume, 1e-12)
            or abs(d_area) > MAX_AREA_NOISE_RATIO * max(owner_area, 1e-12)):
        return {"declined": f"geometry_moved (area {d_area:.6g} mm2, volume {d_volume:.6g} mm3 "
                            "after a split that should move neither)", **measured,
                "area_delta_measured_mm2": _rounded_up(d_area),
                "volume_delta_measured_mm3": _rounded_up(d_volume)}, None
    return measured, divided


def _child_main(argv: list[str]) -> int:
    """`python -m meshpipeline.cad.repair.gap_split <owner.brep> <request json> <out.brep>`."""
    from OCP.TopAbs import TopAbs_FACE

    from meshpipeline.cad.repair.localize import _indexed
    source, request, result = Path(argv[0]), json.loads(argv[1]), Path(argv[2])
    owner = read_brep(source)
    faces = _indexed(owner, TopAbs_FACE)
    index = int(request["face"])
    face = faces.FindKey(index) if 1 <= index <= faces.Extent() else None
    # THE SAME FACE OR NOTHING: the index is read on a BREP round-trip of the owner, so the face it
    # names is checked against the box the parent sent before anything is split.
    if face is None or any(abs(a - b) > 1e-6 * max(1.0, float(request["diagonal_mm"]))
                           for a, b in zip(_box_of(face), request["box"])):
        answer: dict = {"declined": "not_resolved (the target face did not survive the hand-off)"}
    else:
        answer, divided = _work(owner, face, diagonal_mm=float(request["diagonal_mm"]),
                                part_area_mm2=float(request["part_area_mm2"]))
        if divided is not None:
            write_brep(divided, result)
    sys.stdout.write(json.dumps(answer) + "\n")
    sys.stdout.flush()
    return 0


def _precise(shape) -> tuple[float, float]:
    """(area, volume) by adaptive integration (relative error 1e-6) - for the guard only."""
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    area, volume = GProp_GProps(), GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, area, 1e-6)
    BRepGProp.VolumeProperties_s(shape, volume, 1e-6)
    return abs(float(area.Mass())), abs(float(volume.Mass()))


def _is_solid(shape) -> bool:
    from OCP.TopAbs import TopAbs_SOLID

    return shape.ShapeType() == TopAbs_SOLID


if __name__ == "__main__":  # pragma: no cover - the child's own entry point
    raise SystemExit(_child_main(sys.argv[1:]))
