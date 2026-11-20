# Responsibility: Measure how far a repaired surface moved from the upload - the sampled maximum
#                 distance from points on the changed or added faces of the repaired shape to the
#                 original. EVIDENCE for a reviewer, never a gate: nothing may refuse on it (spec
#                 "Owner Answers" 2), so every failure is None rather than a raise or a guess.
#                 `before_shape` is THE UPLOAD as repair received it - the session passes its own
#                 original shape, never a package's starting shape, so packages cannot each measure
#                 small while together moving the part far.
from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from meshpipeline.cad.repair.identity import fingerprint_of, quantum_for

#: WHICH FACES A DEVIATION WAS SAMPLED ON (`DeviationResult.basis`), so a reader can weigh it:
#: the faces the kept operations named as added or rebuilt; failing that, every face of the result
#: the upload does not have; failing that, after a REMOVAL, the faces now covering the removed
#: faces' boxes; failing that, a sparse sample of the whole part. Only a removal reaches the last
#: two - a package that removed nothing and changed no face still reads "no faces".
BASIS_EDITED = "edited_faces"
BASIS_CHANGED = "changed_faces"
BASIS_ADJACENT = "adjacent_to_removed"
BASIS_WHOLE = "whole_part"

#: How far, as a fraction of the part's diagonal, a face's box may sit from a removed face's box
#: and still be taken as one that took over its region (box contact, plus float room).
_ADJACENT_RATIO = 1e-4

#: Fewest samples any selected face gets, so a small face is never left unmeasured by the area
#: weighting giving it a share below one.
_MIN_PER_FACE = 4


def _named_faces(shape, *, quantum: float) -> list[tuple[str, object]]:
    """Every face of `shape` with its fingerprint.

    THE ONE NAMING METHOD, shared with `operations._face_names` and `_split_fingerprints`: the
    face's bounding box through `operations.box_of`, named by `identity.fingerprint_of` at the
    WHOLE PART's quantum. A different method here would name faces differently from the
    `added`/`rebuilt` lists the session hands in, and the filter would select nothing.
    """
    from OCP.TopAbs import TopAbs_FACE

    from meshpipeline.cad.repair.localize import _indexed
    from meshpipeline.cad.repair.operations import box_of

    faces = _indexed(shape, TopAbs_FACE)
    out: list[tuple[str, object]] = []
    for i in range(1, faces.Extent() + 1):
        face = faces.FindKey(i)
        out.append((fingerprint_of("face", box_of(face), quantum=quantum), face))
    return out


def _select_faces(before_shape, after_shape, faces: tuple[str, ...],
                  diagonals: tuple[float, ...] = ()) -> list:
    """The `after_shape` faces to sample: those named in `faces`, or - when it is empty - every
    face whose fingerprint `before_shape` does not have (changed or added).

    WHICH QUANTUM NAMES THE FACES. `faces` come from operations, which name them at the diagonal
    of THEIR OWN INPUT shape (`operations._diagonal_of(shape)`), and that diagonal drifts a little
    as a package's operations rewrite the part. So the caller passes those input diagonals and a
    face is selected when its name at ANY of them is in `faces`; a face named at one quantum and
    looked up at another would silently select nothing. With no `diagonals` (and for the
    empty-`faces` churn fallback, where both sides must share one quantum) the diagonal of
    `before_shape` is used.
    """
    from meshpipeline.cad.repair.operations import _diagonal_of

    base = quantum_for(_diagonal_of(before_shape))
    wanted = set(faces)
    if faces:
        quanta = [quantum_for(d) for d in diagonals] or [base]
        per_quantum = [_named_faces(after_shape, quantum=q) for q in dict.fromkeys(quanta)]
        # Same face order at every quantum, so index i is the same face in each list.
        return [per_quantum[0][i][1] for i in range(len(per_quantum[0]))
                if any(named[i][0] in wanted for named in per_quantum)]
    existing = {name for name, _ in _named_faces(before_shape, quantum=base)}
    return [face for name, face in _named_faces(after_shape, quantum=base)
            if name and name not in existing]


def _removed_boxes(removed) -> list[tuple[tuple, tuple]]:
    """The boxes of the faces kept operations REMOVED, each named on the shape it was removed
    from, at that shape's quantum - the naming `FixSmallFaces` used for `removed_fingerprints`."""
    from meshpipeline.cad.repair.operations import _diagonal_of, box_of

    boxes: list[tuple[tuple, tuple]] = []
    for shape, fingerprints in removed:
        wanted = set(fingerprints)
        if not wanted:
            continue
        for name, face in _named_faces(shape, quantum=quantum_for(_diagonal_of(shape))):
            if name in wanted:
                box = box_of(face)
                boxes.append((tuple(box["bbox_min"]), tuple(box["bbox_max"])))
    return boxes


def _adjacent_faces(after_shape, boxes) -> list:
    """The faces of `after_shape` whose box meets a removed face's box: the faces that took over
    the region a removal emptied."""
    from OCP.TopAbs import TopAbs_FACE

    from meshpipeline.cad.repair.localize import _indexed
    from meshpipeline.cad.repair.operations import _diagonal_of, box_of

    room = _ADJACENT_RATIO * _diagonal_of(after_shape)
    faces = _indexed(after_shape, TopAbs_FACE)
    out = []
    for i in range(1, faces.Extent() + 1):
        face = faces.FindKey(i)
        box = box_of(face)
        low, high = box["bbox_min"], box["bbox_max"]
        if any(all(low[k] <= hi[k] + room and lo[k] <= high[k] + room for k in range(3))
               for lo, hi in boxes):
            out.append(face)
    return out


def _all_faces(shape) -> list:
    from OCP.TopAbs import TopAbs_FACE

    from meshpipeline.cad.repair.localize import _indexed

    faces = _indexed(shape, TopAbs_FACE)
    return [faces.FindKey(i) for i in range(1, faces.Extent() + 1)]


def _face_points(face, count: int) -> list[tuple[float, float, float]]:
    """`count` points spread uniformly in UV over the trimmed bounds of `face`, kept only when
    the point lies on the trimmed face itself (a UV box over-covers a trimmed face)."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepClass import BRepClass_FaceClassifier
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Pnt2d
    from OCP.TopAbs import TopAbs_OUT
    from OCP.TopoDS import TopoDS

    face = TopoDS.Face_s(face)
    umin, umax, vmin, vmax = BRepTools.UVBounds_s(face)
    surface = BRepAdaptor_Surface(face)
    tol = BRep_Tool.Tolerance_s(face)
    side = max(2, math.ceil(math.sqrt(count)))
    points: list[tuple[float, float, float]] = []
    for i in range(side):
        for j in range(side):
            u = umin + (umax - umin) * (i + 0.5) / side
            v = vmin + (vmax - vmin) * (j + 0.5) / side
            if BRepClass_FaceClassifier(face, gp_Pnt2d(u, v), tol).State() == TopAbs_OUT:
                continue
            p = surface.Value(u, v)
            points.append((p.X(), p.Y(), p.Z()))
    return points


def _sample_points(selected: list, samples: int) -> tuple[list, int, int]:
    """Points over `selected`, `samples` shared out by face area with at least `_MIN_PER_FACE`
    each, as (points, faces measured, faces skipped). A face with no in-face point is skipped and
    COUNTED, never silently dropped."""
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    areas = []
    for face in selected:
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        areas.append(max(props.Mass(), 0.0))
    total = sum(areas)
    points: list[tuple[float, float, float]] = []
    measured = skipped = 0
    for face, area in zip(selected, areas, strict=True):
        share = round(samples * area / total) if total > 0 else samples // len(selected)
        got = _face_points(face, max(_MIN_PER_FACE, share))
        points.extend(got)
        measured, skipped = (measured + 1, skipped) if got else (measured, skipped + 1)
    return points, measured, skipped


def _distance_to(before_shape) -> Callable[[tuple[float, float, float]], float]:
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.gp import gp_Pnt

    def distance(point: tuple[float, float, float]) -> float:
        vertex = BRepBuilderAPI_MakeVertex(gp_Pnt(*point)).Vertex()
        measure = BRepExtrema_DistShapeShape(vertex, before_shape)
        if not measure.IsDone():
            raise RuntimeError("distance not computed")
        return float(measure.Value())

    return distance


@dataclass(frozen=True, slots=True)
class DeviationResult:
    """A deviation measurement WITH how sparse it is, so a reader can weigh the number.

    `mm` is None when nothing was measured. `faces == 0` with `mm` None is MEASURED-EMPTY (the
    selection found nothing to sample); `faces is None` is a failure or a spent budget.
    """

    mm: float | None = None
    samples: int = 0            # points actually measured
    faces: int | None = None    # faces actually measured (>= 1 point each)
    faces_skipped: int = 0      # selected faces that yielded no in-face point and were dropped
    seconds: float = 0.0
    basis: str = ""             # which faces were sampled (BASIS_*); "" when nothing was


def measure_deviation(before_shape, after_shape, faces: tuple[str, ...] = (), *,
                      samples: int = 32, budget_s: float = 10.0,
                      diagonals: tuple[float, ...] = (),
                      removed: tuple = (),
                      sampler: Callable | None = None,
                      distance: Callable | None = None,
                      clock: Callable[[], float] = time.monotonic) -> DeviationResult:
    """`surface_deviation_mm` with its sparseness. See that function for the contract.

    `removed` is `((shape_removed_from, fingerprints), ...)` for the faces the kept operations
    REMOVED. A removal declares no added or rebuilt face and may leave every surviving face's box
    as it was, so without it a face-removing package measured nothing and read "no faces" - a
    hole no author could fill. With it, the selection falls back to the faces now covering the
    removed boxes, then to a whole-part sample; `basis` says which (see BASIS_*).

    `sampler(before, after, faces, samples) -> (points, faces_measured, faces_skipped)` and
    `distance(point) -> mm` are test seams. NEVER RAISES.
    """
    started = clock()
    try:
        deadline = started + float(budget_s)
        basis = BASIS_EDITED if faces else BASIS_CHANGED
        if sampler is None:
            selected = _select_faces(before_shape, after_shape, tuple(faces), tuple(diagonals))
            if not selected and not faces and removed:
                selected, basis = _adjacent_faces(after_shape, _removed_boxes(removed)), \
                    BASIS_ADJACENT
                if not selected:
                    selected, basis = _all_faces(after_shape), BASIS_WHOLE
            points, measured, skipped = _sample_points(selected, int(samples))
        else:
            points, measured, skipped = sampler(before_shape, after_shape, tuple(faces),
                                                int(samples))
        points = list(points)
        if not points:
            # Measured-empty when the selection was real but empty; a failure raises above.
            return DeviationResult(None, 0, measured, skipped, clock() - started, basis)
        measure = distance or _distance_to(before_shape)
        worst = 0.0
        for point in points:
            if clock() > deadline:
                return DeviationResult(None, 0, None, 0, clock() - started)
            worst = max(worst, float(measure(point)))
        if not math.isfinite(worst):
            return DeviationResult(None, 0, None, 0, clock() - started)
        return DeviationResult(worst, len(points), measured, skipped, clock() - started, basis)
    except Exception:  # noqa: BLE001 - evidence, never a gate: any failure is "not measured"
        return DeviationResult(None, 0, None, 0, clock() - started)


def surface_deviation_mm(before_shape, after_shape, faces: tuple[str, ...] = (), *,
                         samples: int = 32, budget_s: float = 10.0,
                         **seams) -> float | None:
    """A sampled LOWER BOUND, in mm, on how far the repaired faces sit from the original shape.

    READ IT AS TWO THINGS IT IS AND TWO IT IS NOT.
      - It is the largest of a finite sample of points on the selected `after_shape` faces, each
        measured to the nearest point of `before_shape`: the true maximum is at least this.
      - It counts NEW surface. A patch filling an opening is entirely new surface, and its
        distance is about the gap's half-width - that is the repair's size, not damage.
      - It is ONE-SIDED. Faces of `before_shape` that were deleted or collapsed are never sampled
        (nothing in `after_shape` stands for them), so a removal measures 0 here.
      - With no `faces` the selection is every after-face whose fingerprint the original lacks,
        which measures fingerprint CHURN (renamed-but-unmoved faces included), not only movement.

    `faces` are fingerprints of `after_shape` faces. `samples` is the share-out TARGET, not a
    total: it is split across the selected faces by area, and every face gets AT LEAST
    `_MIN_PER_FACE` points (rounded up to a square grid), so the points actually used can far
    exceed it with many faces - `measure_deviation` reports the real count.

    None, never a raise or a guess, on failure, an empty selection, or a spent budget: a partial
    maximum is a lower bound presented as a measurement of the whole, so a spent budget gives up
    the number rather than under-report it.
    """
    return measure_deviation(before_shape, after_shape, faces, samples=samples,
                             budget_s=budget_s, **seams).mm
