# Responsibility: Lane C13 - an op's relative thresholds read the PART's size, never a state's control-pole box; and
#                 layer 3 reports the TRUE extent. Measured on dev 263d5e2d5bab: a located defeature made B-spline faces
#                 whose poles sit far outside them, so plain BRepBndLib.Add said 1237 mm for a 446.5 mm state (2.8x). A
#                 threshold read from such a box drifts with pole distance, which has no ceiling; bbox_ratio read from it
#                 reports growth that is not there. The fixture here exaggerates on purpose: a 4 mm face on a degree-1
#                 B-spline whose poles sit 10 m out (plain box +-10 m, optimal +-2 mm), so the drift is large enough to
#                 drop a real solid and the test can see it.
# Boundaries: the native tier; real OCCT in spawned workers through the ops' own `apply` and layer 3's kernel.
from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("OCP.BRepAlgoAPI", reason="the CAD kernel is a native-tier dependency")

from meshpipeline.cad.repair_v4.ops import REGISTRY  # noqa: E402
from meshpipeline.cad.repair_v4.verdict.faithful import check_faithful  # noqa: E402
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402
from meshpipeline.cad.repair_v4.workspace import Workspace  # noqa: E402

TIMEOUT = 300


@pytest.fixture
def pool() -> Iterator[WorkerPool]:
    p = WorkerPool(1)
    try:
        yield p
    finally:
        p.close()


def _far_pole_face(z: float, far: float = 10000.0, half: float = 2.0) -> Any:
    """A planar degree-1 B-spline face: 4 control poles `far` mm out, trimmed to a 2*half mm square at the origin."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.Geom import Geom_BSplineSurface
    from OCP.gp import gp_Pnt
    from OCP.TColgp import TColgp_Array2OfPnt
    from OCP.TColStd import TColStd_Array1OfInteger, TColStd_Array1OfReal

    poles = TColgp_Array2OfPnt(1, 2, 1, 2)
    poles.SetValue(1, 1, gp_Pnt(-far, -far, z))
    poles.SetValue(2, 1, gp_Pnt(far, -far, z))
    poles.SetValue(1, 2, gp_Pnt(-far, far, z))
    poles.SetValue(2, 2, gp_Pnt(far, far, z))
    knots = TColStd_Array1OfReal(1, 2)
    knots.SetValue(1, 0.0)
    knots.SetValue(2, 1.0)
    mults = TColStd_Array1OfInteger(1, 2)
    mults.SetValue(1, 2)
    mults.SetValue(2, 2)
    surf = Geom_BSplineSurface(poles, knots, knots, mults, mults, 1, 1)
    t = half / (2 * far)
    return BRepBuilderAPI_MakeFace(surf, 0.5 - t, 0.5 + t, 0.5 - t, 0.5 + t, 1e-7).Face()


def _comp(*shapes: Any) -> Any:
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    c = TopoDS_Compound()
    b = BRep_Builder()
    b.MakeCompound(c)
    for s in shapes:
        b.Add(c, s)
    return c


def _box(x: float, y: float, z: float, d: float) -> Any:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    return BRepPrimAPI_MakeBox(gp_Pnt(x, y, z), d, d, d).Shape()


def _write(shape: Any, path: Path) -> str:
    from OCP.BRepTools import BRepTools

    BRepTools.Write_s(shape, str(path))
    return str(path)


def _read(path: str) -> Any:
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape

    s = TopoDS_Shape()
    BRepTools.Read_s(s, path, BRep_Builder())
    return s


def _solid_volumes(path: str) -> list[float]:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(_read(path), TopAbs_SOLID, m)
    out = []
    for i in range(1, m.Extent() + 1):
        p = GProp_GProps()
        BRepGProp.VolumeProperties_s(m.FindKey(i), p)
        out.append(p.Mass())
    return sorted(out)


def test_the_fixture_face_inflates_the_plain_box_and_not_the_optimal_one() -> None:
    from meshpipeline.cad.repair_v4.ws_kernel import diagonal_of

    face = _far_pole_face(-5.0)
    assert diagonal_of(face) > 20000.0  # plain Add: the poles
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    b = Bnd_Box()
    BRepBndLib.AddOptimal_s(face, b, False, False)
    x0, y0, _, x1, y1, _ = b.Get()
    assert abs(x1 - x0 - 4.0) < 1e-3 and abs(y1 - y0 - 4.0) < 1e-3  # the face itself


def _states(pool: WorkerPool, tmp_path: Path) -> tuple[Workspace, Workspace]:
    """A keeper A (20 mm cube) and a victim B (16 mm cube) poking 1 mm out of it on three sides, so cut_by:A leaves a
    REAL 721 mm3 piece of B. The ORIGINAL has no far-pole face (diag ~36 mm); the STATE an op made has one beside
    the bodies (plain diag ~28 m), as the located defeature's B-splines did on 263d5e2d5bab."""
    a, b = _box(0, 0, 0, 20), _box(5, 5, 5, 16)
    original = Workspace.from_brep(pool, _write(_comp(_comp(a, b)), tmp_path / "original.brep"))
    state = Workspace.from_brep(pool, _write(_comp(_comp(a, b), _far_pole_face(-5.0)), tmp_path / "state.brep"))
    assert state.part_diag_mm is not None and state.part_diag_mm > 20000.0  # what the state's own box says
    assert original.part_diag_mm is not None and original.part_diag_mm < 40.0
    return original, dataclasses.replace(state, part_diag_mm=original.part_diag_mm)  # the lineage's size


def test_a_real_piece_survives_a_cut_on_a_state_with_a_far_pole_box(pool: WorkerPool, tmp_path: Path) -> None:
    """LEAD-2's case: on a 28 m pole box the zero-volume eps is ~22,600 mm3, so the 721 mm3 piece of B - a real solid
    - was dropped (and the 3,375 mm3 overlap itself read as 'touching'). Sized from the part, it survives."""
    _, state = _states(pool, tmp_path)
    bodies = sorted((s for s in state.ids if s[0] == "B"), key=lambda s: int(s[1:]))
    new, diff = REGISTRY["conformalise"].apply(pool, state, bodies, {"mode": f"cut_by:{bodies[0]}"},
                                               timeout_s=TIMEOUT)
    assert diff.params.get("declined") is None, diff.params
    measured = diff.params["measured"]
    assert measured["zero_volume_pieces_dropped"] == []
    assert [round(o["volume_mm3"]) for o in measured["overlaps"]] == [3375]
    vols = _solid_volumes(new.brep_path)
    assert len(vols) == 2 and vols[0] == pytest.approx(16 ** 3 - 15 ** 3, rel=1e-6) and vols[1] == pytest.approx(8000)


def test_without_the_parts_size_the_same_cut_loses_the_real_piece(pool: WorkerPool, tmp_path: Path) -> None:
    """The hazard itself, kept visible: a caller that sends no `part_diag_mm` falls back to the shape's own (pole)
    box, and the same cut no longer resolves the overlap correctly."""
    _, state = _states(pool, tmp_path)
    blind = dataclasses.replace(state, part_diag_mm=None)
    bodies = sorted((s for s in blind.ids if s[0] == "B"), key=lambda s: int(s[1:]))
    new, diff = REGISTRY["conformalise"].apply(pool, blind, bodies, {"mode": f"cut_by:{bodies[0]}"}, timeout_s=TIMEOUT)
    measured = (diff.params or {}).get("measured") or {}
    right = (diff.params.get("declined") is None and measured.get("zero_volume_pieces_dropped") == []
             and len(_solid_volumes(new.brep_path)) == 2)
    assert not right, "the pole box no longer misleads the fallback: this test's premise is gone"


def test_layer_3_reports_the_true_extent_not_the_pole_box(pool: WorkerPool, tmp_path: Path) -> None:
    """bbox_ratio and diag_after_mm compare OPTIMAL boxes: a 4 mm face whose poles sit 10 m out does not make a 20 mm
    part 'grow' 1400x. The deviation bound still uses the original's plain box, so no faithfulness reason moves."""
    part = _box(-10, -10, 0, 20)  # the 4 mm face (at the origin, z=5) lies INSIDE it: no real growth
    original = _write(_comp(part), tmp_path / "o.brep")
    repaired = _write(_comp(part, _far_pole_face(5.0)), tmp_path / "r.brep")
    report = check_faithful(pool, original, repaired, declared_added=[], declared_removed=[])
    assert report.bbox_ratio is not None and report.bbox_ratio == pytest.approx(1.0, abs=1e-6)
    assert report.diag_after_mm == pytest.approx((3 * 20 ** 2) ** 0.5, rel=1e-6)
