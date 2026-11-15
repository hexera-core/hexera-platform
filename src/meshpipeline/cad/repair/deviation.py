# Responsibility: Measure how far a repaired surface moved from the upload - the sampled maximum
#                 distance from points on the changed or added faces of the repaired shape to the
#                 original. EVIDENCE for a reviewer, never a gate: nothing may refuse on it (spec
#                 "Owner Answers" 2), so every failure is None rather than a raise or a guess.
from __future__ import annotations

import math
import time
from collections.abc import Callable

from meshpipeline.cad.repair.identity import fingerprint_of, quantum_for

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


def _select_faces(before_shape, after_shape, faces: tuple[str, ...]) -> list:
    """The `after_shape` faces to sample: those named in `faces`, or - when it is empty - every
    face whose fingerprint `before_shape` does not have (changed or added)."""
    from meshpipeline.cad.repair.operations import _diagonal_of

    quantum = quantum_for(_diagonal_of(before_shape))
    wanted = set(faces)
    existing = (set() if faces else
                {name for name, _ in _named_faces(before_shape, quantum=quantum)})
    chosen = []
    for name, face in _named_faces(after_shape, quantum=quantum):
        if name and ((name in wanted) if faces else (name not in existing)):
            chosen.append(face)
    return chosen


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


def _sample_points(selected: list, samples: int) -> list[tuple[float, float, float]]:
    """`samples` points over `selected`, shared out by face area with at least `_MIN_PER_FACE`
    each."""
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    areas = []
    for face in selected:
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        areas.append(max(props.Mass(), 0.0))
    total = sum(areas)
    points: list[tuple[float, float, float]] = []
    for face, area in zip(selected, areas, strict=True):
        share = round(samples * area / total) if total > 0 else samples // len(selected)
        points.extend(_face_points(face, max(_MIN_PER_FACE, share)))
    return points


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


def surface_deviation_mm(before_shape, after_shape, faces: tuple[str, ...] = (), *,
                         samples: int = 32, budget_s: float = 10.0,
                         sampler: Callable | None = None,
                         distance: Callable | None = None,
                         clock: Callable[[], float] = time.monotonic) -> float | None:
    """The largest sampled distance, in mm, from the repaired faces to the original shape.

    `faces` are fingerprints of `after_shape` faces (empty: every face `before_shape` lacks).
    NONE, NEVER A RAISE OR A GUESS, when anything fails, nothing was sampled, or the budget ran
    out before every sample was measured: a partial maximum is a lower bound presented as a
    measurement, so a spent budget gives up the number rather than under-report it.

    `sampler(before, after, faces, samples) -> points` and `distance(point) -> mm` are test
    seams; production leaves them None.
    """
    try:
        deadline = clock() + float(budget_s)
        if sampler is None:
            selected = _select_faces(before_shape, after_shape, tuple(faces))
            points = _sample_points(selected, int(samples)) if selected else []
        else:
            points = list(sampler(before_shape, after_shape, tuple(faces), int(samples)))
        if not points:
            return None
        measure = distance or _distance_to(before_shape)
        worst = 0.0
        for point in points:
            if clock() > deadline:
                return None
            worst = max(worst, float(measure(point)))
        return worst if math.isfinite(worst) else None
    except Exception:  # noqa: BLE001 - evidence, never a gate: any failure is "not measured"
        return None
