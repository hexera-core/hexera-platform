# Responsibility: Prove natively that ops decline or fail cleanly where snapshot v1 saw them raise or invalidate.
# Boundaries: in-process kernel helpers on synthetic shapes; the corpus reproductions are in lane-a6-report.md.
from __future__ import annotations

import pytest

pytest.importorskip("OCP.BRepLib", reason="the CAD kernel is a native-tier dependency")

from OCP.BRep import BRep_Builder  # noqa: E402
from OCP.BRepCheck import BRepCheck_Analyzer  # noqa: E402
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox  # noqa: E402
from OCP.TopAbs import TopAbs_EDGE  # noqa: E402
from OCP.TopExp import TopExp  # noqa: E402
from OCP.TopoDS import TopoDS  # noqa: E402
from OCP.TopTools import TopTools_IndexedMapOfShape  # noqa: E402

from meshpipeline.cad.repair_v4 import kernel_calls  # noqa: E402,F401  (import order: as a worker does)
from meshpipeline.cad.repair_v4.ops.split import settle_same_parameter  # noqa: E402


def _edges(shape) -> list:
    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_EDGE, m)
    return [m.FindKey(i) for i in range(1, m.Extent() + 1)]


def test_only_edges_the_split_created_and_brepcheck_rejects_are_settled_in_place() -> None:
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy

    old = BRepPrimAPI_MakeBox(10.0, 20.0, 30.0).Shape()
    new = BRepBuilderAPI_Copy(old).Shape()  # every edge a NEW TShape, as a split's are
    edges = _edges(new)
    BRep_Builder().SameParameter(TopoDS.Edge_s(edges[3]), False)
    assert not BRepCheck_Analyzer(new).IsValid()
    assert settle_same_parameter(old, new) >= 1
    assert BRepCheck_Analyzer(new).IsValid()
    assert all(a.IsSame(b) for a, b in zip(_edges(new), edges, strict=True))  # in place: the same TShapes


def test_edges_that_were_already_in_the_input_are_never_touched() -> None:
    shape = BRepPrimAPI_MakeBox(10.0, 20.0, 30.0).Shape()
    BRep_Builder().SameParameter(TopoDS.Edge_s(_edges(shape)[3]), False)
    assert settle_same_parameter(shape, shape) == 0  # old == new: nothing was created by an op
    assert not BRepCheck_Analyzer(shape).IsValid()
