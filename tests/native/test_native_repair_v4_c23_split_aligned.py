# Responsibility: lane C23 - `split_aligned` imprints half-plane sections so near-coaxial cylinders are split on the
#                 SAME rays (their meshers' polygons then nest), as a pure split: no point moves, volume unchanged.
# Boundaries: the native tier; real OCCT in a spawned worker; fixture built here (a shaft through a bore in another body,
#             0.254 mm radial clearance, as on dev f9501251fb30 and its six ABC duplicates).
from __future__ import annotations

import math
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("OCP.BRepAlgoAPI", reason="the CAD kernel is a native-tier dependency")

from meshpipeline.cad.repair_v4.ops import REGISTRY, OpDeclined  # noqa: E402
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402
from meshpipeline.cad.repair_v4.workspace import Workspace  # noqa: E402

TIMEOUT = 600


@pytest.fixture(scope="module")
def pool() -> Iterator[WorkerPool]:
    p = WorkerPool(1)
    try:
        yield p
    finally:
        p.close()


def _part(path: Path) -> str:
    """A 12.192 mm shaft along x through a block with a 12.7 mm bore: 0.254 mm radial clearance, two bodies."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.TopoDS import TopoDS_Compound

    axis = gp_Ax2(gp_Pnt(-50, 0, 0), gp_Dir(1, 0, 0))
    shaft = BRepPrimAPI_MakeCylinder(axis, 6.096, 100.0).Shape()
    block = BRepAlgoAPI_Cut(BRepPrimAPI_MakeBox(gp_Pnt(-16, -20, -20), 32, 40, 40).Shape(),
                            BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(-60, 0, 0), gp_Dir(1, 0, 0)), 6.35, 120.0).Shape()
                            ).Shape()
    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    builder.Add(compound, shaft)
    builder.Add(compound, block)
    assert BRepTools.Write_s(compound, str(path))
    return str(path)


def _cylinder_edges_angles(brep: str) -> dict[str, set[float]]:
    """The angles (degrees, around the x axis) of the straight edges on the shaft (r ~6.096) and on the bore (~6.35)."""
    from OCP.BRep import BRep_Builder, BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepTools import BRepTools
    from OCP.GeomAbs import GeomAbs_Line
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS, TopoDS_Shape
    from OCP.TopTools import TopTools_IndexedMapOfShape

    shape = TopoDS_Shape()
    BRepTools.Read_s(shape, brep, BRep_Builder())
    edges = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_EDGE, edges)
    out: dict[str, set[float]] = {"shaft": set(), "bore": set()}
    for i in range(1, edges.Extent() + 1):
        e = TopoDS.Edge_s(edges.FindKey(i))
        if BRep_Tool.Degenerated_s(e) or BRepAdaptor_Curve(e).GetType() != GeomAbs_Line:
            continue
        p = BRepAdaptor_Curve(e).Value(BRepAdaptor_Curve(e).FirstParameter())
        r = math.hypot(p.Y(), p.Z())
        if 6.0 < r < 6.4:
            out["shaft" if r < 6.2 else "bore"].add(round(math.degrees(math.atan2(p.Z(), p.Y())) % 360.0, 3))
    return out


def _volume(brep: str) -> float:
    from OCP.BRep import BRep_Builder
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepTools import BRepTools
    from OCP.GProp import GProp_GProps
    from OCP.TopoDS import TopoDS_Shape

    shape = TopoDS_Shape()
    BRepTools.Read_s(shape, brep, BRep_Builder())
    g = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, g)
    return g.Mass()


def test_all_scope_splits_the_shaft_and_the_bore_on_the_same_rays(pool: WorkerPool, tmp_path: Path) -> None:
    ws = Workspace.from_brep(pool, _part(tmp_path / "shaft_bore.brep"))
    new, diff = REGISTRY["split_aligned"].apply(pool, ws, [], {"segments": 8, "scope": "all"}, timeout_s=TIMEOUT)
    assert diff.params.get("declined") is None and diff.params["groups"] == 1 and diff.params["faces"] == 2
    before = _cylinder_edges_angles(ws.brep_path)
    after = _cylinder_edges_angles(new.brep_path)
    # the 8 split rays are COMMON to the shaft and the bore (each may also keep its own seam ray)
    common = after["shaft"] & after["bore"]
    assert len(common) >= 8, after
    assert len(after["shaft"] - before["shaft"]) >= 7 and len(after["bore"] - before["bore"]) >= 7
    assert _volume(new.brep_path) == pytest.approx(_volume(ws.brep_path), rel=1e-9)  # no point moved


def test_faces_that_are_not_cylinders_or_cones_are_declined(pool: WorkerPool, tmp_path: Path) -> None:
    ws = Workspace.from_brep(pool, _part(tmp_path / "shaft_bore.brep"))
    planar = next(s for s, i in sorted(ws.indices.items(), key=lambda kv: kv[1]) if s[0] == "F" and i == 2)
    with pytest.raises(OpDeclined, match="not_revolved"):
        REGISTRY["split_aligned"].apply(pool, ws, [planar], {"segments": 8}, timeout_s=TIMEOUT)
