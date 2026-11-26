# Responsibility: the kernel side of v5's CONFORMAL ASSEMBLY PASS: measure every body pair's gap/overlap
#                 (`v5_body_gaps`) and translate one body onto a near-coincident partner (`v5_tweak`).
# Owns: the two registered kernel functions.
# Boundaries: runs INSIDE a worker (OCP imported lazily). Measures and moves; decides nothing - the bounds, the fuzzy
#             schedule and the keep/revert rule are `conformal.py`'s. The workspace record is built by body_kernel's
#             `_finish`, the same assembly every body op uses.
"""Body-pair measurement and the `tweak` op's kernel.

`v5_body_gaps`: for each pair of solids whose boxes come within `reach_mm`: the exact gap (BRepExtrema), whether the
closest points lie inside two FACES whose normals are parallel (a near-coincident face pair - what a misalignment
is), and, for pairs at gap ~0, the common volume (overlap) and both body volumes, so the parent can choose the
Boolean by volume. Bounded by a wall budget and a pair cap; a truncated answer says so.

`v5_tweak`: translate ONE body by the vector between the closest points so its near-coincident face lands on its
partner's (the gap closes to contact). Declines when the bodies already touch, when the gap is over `max_mm`, when
the closest faces are not parallel (a translation would not make them coincide), or when the moved body would then
overlap its partner. Every entity of the moved body is reported modified 1:1; the moved distance is the deviation.
"""

from __future__ import annotations

import math
import time
from typing import Any

from meshpipeline.cad.repair_v4.kernel_calls import register

CALL_BUDGET_S = 60.0
MAX_PAIRS = 400
#: two faces are near-coincident when their normals at the closest points are within this angle (degrees)
PARALLEL_DEG = 1.0
#: a gap at or under this is contact (= sites_kernel.CONTACT_MM)
CONTACT_MM = 1e-7


def _box(shape: Any, enlarge: float) -> Any:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    box.Enlarge(enlarge)
    return box


def _face_normal(face: Any, point: Any) -> Any | None:
    from OCP.BRep import BRep_Tool
    from OCP.BRepGProp import BRepGProp_Face
    from OCP.gp import gp_Pnt, gp_Vec
    from OCP.ShapeAnalysis import ShapeAnalysis_Surface
    from OCP.TopoDS import TopoDS

    f = TopoDS.Face_s(face)
    uv = ShapeAnalysis_Surface(BRep_Tool.Surface_s(f)).ValueOfUV(point, 1e-7)
    p, n = gp_Pnt(), gp_Vec()
    BRepGProp_Face(f).Normal(uv.X(), uv.Y(), p, n)
    return n if n.Magnitude() > 1e-12 else None


def closest(a: Any, b: Any) -> dict:
    """{gap_mm, point_a, point_b, normal_angle_deg (None unless both closest points are interior to a face)}."""
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape, BRepExtrema_IsInFace

    m = BRepExtrema_DistShapeShape(a, b)
    if not m.IsDone() or m.NbSolution() < 1:
        return {"gap_mm": None, "point_a": None, "point_b": None, "normal_angle_deg": None}
    pa, pb = m.PointOnShape1(1), m.PointOnShape2(1)
    angle = None
    if m.SupportTypeShape1(1) == BRepExtrema_IsInFace and m.SupportTypeShape2(1) == BRepExtrema_IsInFace:
        try:
            na, nb = _face_normal(m.SupportOnShape1(1), pa), _face_normal(m.SupportOnShape2(1), pb)
        except Exception:  # noqa: BLE001 - an unmeasurable normal is "not known to be parallel", never a crash
            na = nb = None
        if na is not None and nb is not None:
            raw = math.degrees(na.Angle(nb))
            angle = round(min(raw, 180.0 - raw), 4)
    return {"gap_mm": float(m.Value()), "point_a": [pa.X(), pa.Y(), pa.Z()], "point_b": [pb.X(), pb.Y(), pb.Z()],
            "normal_angle_deg": angle}


@register("v5_body_gaps")
def v5_body_gaps(payload: dict) -> dict:
    """`{"pairs": [{a, b, gap_mm, parallel, normal_angle_deg, overlap_mm3?}], "volumes": {i: mm3}, "truncated",
    "diagonal_mm", "bodies"}` over every solid pair whose boxes come within `reach_mm`. Indices are kernel solid
    indices (1-based). `overlap_mm3` is measured for pairs at contact only (a gap > 0 cannot share volume)."""
    from meshpipeline.cad.repair_v4.body_kernel import _common_volume, _volume, zero_volume_mm3
    from meshpipeline.cad.repair_v4.ws_kernel import _maps, part_diagonal, read_brep

    deadline = time.monotonic() + float(payload.get("budget_s", CALL_BUDGET_S))
    shape = read_brep(payload["brep_path"])
    solids = _maps(shape)["solid"]
    n = solids.Extent()
    reach = float(payload["reach_mm"])
    diag = part_diagonal(payload, shape)
    zero = zero_volume_mm3(diag)
    boxes = [_box(solids.FindKey(i), reach) for i in range(1, n + 1)]
    volumes = {i: abs(_volume(solids.FindKey(i))) for i in range(1, n + 1)}
    out: list[dict] = []
    truncated = ""
    for i in range(1, n + 1):
        for j in range(i + 1, n + 1):
            if boxes[i - 1].IsOut(boxes[j - 1]):
                continue
            if len(out) >= MAX_PAIRS:
                truncated = f"over {MAX_PAIRS} pairs"
                break
            if time.monotonic() > deadline:
                truncated = "over the call budget"
                break
            a, b = solids.FindKey(i), solids.FindKey(j)
            facts = closest(a, b)
            if facts["gap_mm"] is None or facts["gap_mm"] > reach:
                continue
            row: dict[str, Any] = {"a": i, "b": j, "gap_mm": round(facts["gap_mm"], 9),
                                   "normal_angle_deg": facts["normal_angle_deg"],
                                   "parallel": facts["normal_angle_deg"] is not None
                                   and facts["normal_angle_deg"] <= PARALLEL_DEG}
            if facts["gap_mm"] <= CONTACT_MM:
                try:
                    common = _common_volume(a, b, 0.0)
                except Exception:  # noqa: BLE001 - the Boolean failed: say so, the parent will not Boolean it
                    common = None
                row["overlap_mm3"] = None if common is None else (common if common > zero else 0.0)
            out.append(row)
        if truncated:
            break
    return {"pairs": out, "volumes": {str(k): v for k, v in volumes.items()}, "truncated": truncated,
            "diagonal_mm": diag, "bodies": n}


@register("v5_tweak")
def v5_tweak(payload: dict) -> dict:
    """Translate solid `moved` (kernel index) onto solid `partner` by the closest-point vector, when the gap is in
    (CONTACT_MM, max_mm] and the closest faces are parallel. See the module docstring for the declines."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.BRepTools import BRepTools_History
    from OCP.gp import gp_Trsf, gp_Vec

    from meshpipeline.cad.repair_v4.body_kernel import (
        _KINDS,
        _common_volume,
        _declined,
        _finish,
        zero_volume_mm3,
    )
    from meshpipeline.cad.repair_v4.ws_kernel import _maps, part_diagonal, read_brep

    start = time.perf_counter()
    old = read_brep(payload["brep_path"])
    solids = _maps(old)["solid"]
    moved, partner = solids.FindKey(int(payload["moved"])), solids.FindKey(int(payload["partner"]))
    facts = closest(moved, partner)
    gap = facts["gap_mm"]
    if gap is None:
        return _declined("unmeasured", detail="BRepExtrema found no distance between the bodies")
    if gap <= CONTACT_MM:
        return _declined("touching", gap_mm=gap)
    if gap > float(payload["max_mm"]):
        return _declined("gap_over_bound", gap_mm=gap, max_mm=float(payload["max_mm"]))
    angle = facts["normal_angle_deg"]
    if angle is None or angle > PARALLEL_DEG:
        return _declined("not_coincident", gap_mm=gap, normal_angle_deg=angle,
                         detail="the closest points are not on two parallel faces: a translation would not make "
                                "them coincide")
    pa, pb = facts["point_a"], facts["point_b"]
    trsf = gp_Trsf()
    trsf.SetTranslation(gp_Vec(pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2]))
    maker = BRepBuilderAPI_Transform(moved, trsf, True)
    if not maker.IsDone():
        return _declined("transform_failed")
    new_solid = maker.Shape()
    diag = part_diagonal(payload, old)
    if _common_volume(new_solid, partner, 0.0) > zero_volume_mm3(diag):
        return _declined("would_overlap", gap_mm=gap,
                         detail="the translated body would share volume with its partner")
    hist = BRepTools_History()
    maps = _maps(moved)
    for kind in _KINDS:
        for i in range(1, maps[kind].Extent() + 1):
            sub = maps[kind].FindKey(i)
            hist.AddModified(sub, maker.ModifiedShape(sub))
    params = {"bodies": payload.get("body_ids", []), "max_mm": float(payload["max_mm"]),
              "measured": {"moved_mm": gap, "normal_angle_deg": angle,
                           "vector": [round(pb[k] - pa[k], 12) for k in range(3)]}}
    return _finish(payload, old, [(hist, moved)], [moved], [new_solid], op="tweak", params=params, start=start)
