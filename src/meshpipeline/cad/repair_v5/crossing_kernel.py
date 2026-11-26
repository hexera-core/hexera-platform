# Responsibility: the kernel measurement behind v5's SURFACE CROSSING detectors (step 9): for the faces at a crossing
#                 defect, every face touching or crossing them, measured as a pair.
# Owns: `v5_crossing_facts` (registered kernel fn).
# Boundaries: measures, never decides: `repair_v5.crossing` turns the rows into a crossing kind (duplicate faces,
#             near-coincident faces, a true crossing). The pair geometry is diagnose_kernel's (`pair_facts_of`), the
#             interior section is the imprint op's own (`ops.crossing.crossing_edges`), read here, not copied.
"""Crossing facts. For each named face, every face whose box meets it within `tol_mm` and that shares no edge with it:
gap, normal angle at the closest points, both surface types, both AREAS and the largest difference between the two
faces' boxes (what a duplicate-face test needs), the owning solids, and the length of their section through the
interior of either face (what a true crossing has and a mere touch does not)."""

from __future__ import annotations

import time
from typing import Any

from meshpipeline.cad.repair_v4.kernel_calls import register

CALL_BUDGET_S = 45.0
MAX_NAMED_FACES = 12
MAX_PARTNERS_PER_FACE = 8


def _area(face: Any) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    return abs(float(props.Mass()))


def _box(face: Any, enlarge: float = 0.0) -> Any:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    b = Bnd_Box()
    BRepBndLib.AddOptimal_s(face, b, False, False)
    if enlarge:
        b.Enlarge(enlarge)
    return b


@register("v5_crossing_facts")
def v5_crossing_facts(payload: dict) -> dict:
    """`{"pairs": [{a, b, solid_a, solid_b, surface_a, surface_b, gap_mm, normal_angle_deg, area_a_mm2, area_b_mm2,
    box_dev_mm, section_mm}], "truncated", "diagonal_mm"}` for the named kernel face indices (`faces`)."""
    from OCP.TopoDS import TopoDS

    from meshpipeline.cad.repair_v4.ops.crossing import crossing_edges
    from meshpipeline.cad.repair_v4.ws_kernel import _maps, part_diagonal, read_brep
    from meshpipeline.cad.repair_v5.diagnose_kernel import _closest, _kind, _shares_edge, _solid_of

    deadline = time.monotonic() + CALL_BUDGET_S
    shape = read_brep(payload["brep_path"])
    maps = _maps(shape)
    faces = maps["face"]
    diag = part_diagonal(payload, shape)
    tol = float(payload["tol_mm"])
    solid = _solid_of(maps)
    boxes: dict[int, Any] = {}

    def box(i: int) -> Any:
        if i not in boxes:
            boxes[i] = _box(faces.FindKey(i))
        return boxes[i]

    out: list[dict] = []
    seen: set[tuple[int, int]] = set()
    truncated = False
    for a in [int(x) for x in payload["faces"]][:MAX_NAMED_FACES]:
        if not 1 <= a <= faces.Extent():
            continue
        fa = TopoDS.Face_s(faces.FindKey(a))
        reach = _box(fa, tol)
        found = 0
        for b in range(1, faces.Extent() + 1):
            key = (min(a, b), max(a, b))
            if b == a or key in seen or reach.IsOut(box(b)):
                continue
            if time.monotonic() > deadline:
                truncated = True
                break
            fb = TopoDS.Face_s(faces.FindKey(b))
            if _shares_edge(fa, fb):
                continue
            near = _closest(fa, fb)
            if near["gap_mm"] is None or near["gap_mm"] > tol:
                continue
            seen.add(key)
            ba, bb = box(a).Get(), box(b).Get()
            row: dict[str, Any] = {
                "a": a, "b": b, "solid_a": solid.get(a), "solid_b": solid.get(b),
                "surface_a": _kind(fa), "surface_b": _kind(fb), "gap_mm": near["gap_mm"],
                "normal_angle_deg": near["normal_angle_deg"], "point_a": near["point_a"],
                "area_a_mm2": round(_area(fa), 9), "area_b_mm2": round(_area(fb), 9),
                "box_dev_mm": round(max(abs(x - y) for x, y in zip(ba, bb, strict=True)), 9)}
            try:
                _, crossed = crossing_edges(shape, [a], max_partners=1, diag=diag, only=b)
                row["section_mm"] = round(sum(r["length_mm"] for r in crossed), 6)
            except Exception:  # noqa: BLE001 - an unmeasured section degrades the row, never loses it
                row["section_mm"] = None
            out.append(row)
            found += 1
            if found >= MAX_PARTNERS_PER_FACE:
                break
        if truncated:
            break
    return {"pairs": out, "truncated": truncated, "diagonal_mm": round(diag, 6)}
