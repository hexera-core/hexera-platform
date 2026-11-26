"""D18 on the real kernel: a mesher's located failure on a face BESIDE a 300:1 sliver gets unify_same_domain and
remove_small aimed at the sliver, and both remove it.

Lane A11's caveat made concrete: the low_quality location names the big face, and the sliver is its neighbour.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("OCP.BRepFeat", reason="the CAD kernel is a native-tier dependency")

from meshpipeline.cad.repair_v4.ops import REGISTRY  # noqa: E402
from meshpipeline.cad.repair_v4.search import DetGenerator, SearchState  # noqa: E402
from meshpipeline.cad.repair_v4.sites import find_sites  # noqa: E402
from meshpipeline.cad.repair_v4.verdict.assemble import Verdict  # noqa: E402
from meshpipeline.cad.repair_v4.verdict.panel import Located, MemberResult  # noqa: E402
from meshpipeline.cad.repair_v4.verdict.valid import ValidReport  # noqa: E402
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402
from meshpipeline.cad.repair_v4.workspace import Workspace  # noqa: E402


@pytest.fixture
def pool() -> Iterator[WorkerPool]:
    p = WorkerPool(1)
    try:
        yield p
    finally:
        p.close()


def _slivered_block() -> Any:
    """A 60 x 40 x 20 block whose top face is split 0.2 mm from its y = 0 side: a 60 x 0.2 strip (300:1)."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
    from OCP.BRepFeat import BRepFeat_SplitShape
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    box = BRepPrimAPI_MakeBox(60.0, 40.0, 20.0).Shape()
    top = None
    e = TopExp_Explorer(box, TopAbs_FACE)
    while e.More():
        f = TopoDS.Face_s(e.Current())
        g = GProp_GProps()
        BRepGProp.SurfaceProperties_s(f, g)
        if abs(g.CentreOfMass().Z() - 20.0) < 1e-9:
            top = f
        e.Next()
    split = BRepFeat_SplitShape(box)
    split.Add(BRepBuilderAPI_MakeEdge(gp_Pnt(0, 0.2, 20), gp_Pnt(60, 0.2, 20)).Edge(), top)
    split.Build()
    return split.Shape()


def _faces(shape: Any) -> list[tuple[float, float]]:
    """(area, z of the centre) of every face."""
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer

    out = []
    e = TopExp_Explorer(shape, TopAbs_FACE)
    while e.More():
        g = GProp_GProps()
        BRepGProp.SurfaceProperties_s(e.Current(), g)
        out.append((g.Mass(), g.CentreOfMass().Z()))
        e.Next()
    return out


def _read(path: str) -> Any:
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape

    s = TopoDS_Shape()
    BRepTools.Read_s(s, path, BRep_Builder())
    return s


def test_a_low_quality_point_beside_a_sliver_gets_both_ops_aimed_at_the_sliver(pool: WorkerPool,
                                                                              tmp_path: Path) -> None:
    from OCP.BRepTools import BRepTools

    shape = _slivered_block()
    assert len(_faces(shape)) == 7 and any(abs(a - 12.0) < 1e-6 for a, _ in _faces(shape))   # the 60 x 0.2 strip
    path = tmp_path / "slivered.brep"
    BRepTools.Write_s(shape, str(path))
    ws = Workspace.from_brep(pool, str(path))
    geo = pool.call("sites_geometry", {"brep_path": ws.brep_path, "entities": [
        {"id": s, "type": "face", "index": ws.indices[s]} for s in ws.ids if s[0] == "F"]}, timeout_s=120).value
    areas = {sid: g["area"] for sid, g in geo["geometry"].items()}
    big = next(s for s, a in areas.items() if abs(a - 60 * 39.8) < 1e-3)
    sliver = next(s for s, a in areas.items() if abs(a - 12.0) < 1e-6)

    # gmsh_alt's low_quality names the BIG top face: the worst element is there, the sliver is its neighbour
    member = MemberResult("gmsh_alt", "fail", "low_quality", "minimum SICN 0.001 is below 0.01",
                          [Located(ws.ids[big], None, "surface 6")])
    verdict = Verdict(ValidReport(True, [], [], "mm", 1, True), [member], None, False, "full")
    sites = [s for s in find_sites(pool, ws, verdict, h_mm=5.0) if big in s.entities]
    assert sites and sites[0].graph["mesher_faces"] == [big]

    gen = DetGenerator()
    gen.bind_pool(pool)
    state = SearchState.__new__(SearchState)
    state.ws, state.sites, state.lineage, state.depth = ws, sites, (), 0  # type: ignore[attr-defined]
    aimed = [c for c in gen.propose(state, 999) if c.op in ("unify_same_domain", "remove_small")
             and "beside the face the mesher named" in c.rationale]
    assert [c.op for c in aimed[:2]] == ["unify_same_domain", "remove_small"]
    assert aimed[0].targets[0] == sliver and aimed[1].targets == [sliver]

    for cand in aimed[:2]:
        new, diff = REGISTRY[cand.op].apply(pool, ws.fork(), list(cand.targets), dict(cand.params), timeout_s=120)
        after = _faces(_read(new.brep_path))
        assert not any(abs(a - 12.0) < 1e-6 for a, _ in after), (cand.op, after)   # the strip is gone
        assert len(after) == 6, (cand.op, after)                                   # back to a plain block
