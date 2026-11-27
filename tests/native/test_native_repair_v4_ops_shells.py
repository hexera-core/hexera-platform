# Responsibility: Prove on the REAL kernel that the shell ops heal what they claim: 6 loose faces
#                 sew (at the tightest working rung) and become a valid solid, an inside-out solid
#                 turns positive, a 0.01 mm sliver is removed AND declared, and identity survives.
# Boundaries: the native tier. STAND-INS for the verdict (Tasks 2-5, not in this branch): layer-1
#             validity is BRepCheck_Analyzer + ShapeAnalysis_FreeBounds + BRepGProp volume sign;
#             fidelity is a two-sided sampled-point deviation against min(0.1 mm, 0.001 x diagonal).
#             Tighten these onto the real verdict when it lands.
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("OCP.BRepAlgoAPI", reason="the CAD kernel is a native-tier dependency")

from meshpipeline.cad.repair_v4.ops import shells  # noqa: E402
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402
from meshpipeline.cad.repair_v4.workspace import Workspace  # noqa: E402

OPS = {op.name: op for op in shells.OPS}
TIMEOUT = 180


@pytest.fixture
def pool() -> Iterator[WorkerPool]:
    p = WorkerPool(1)
    try:
        yield p
    finally:
        p.close()


# --- OCCT measurements (the verdict's stand-ins) -------------------------------------------------

def _write(shape: Any, path: Path) -> str:
    from OCP.BRepTools import BRepTools

    assert BRepTools.Write_s(shape, str(path))
    return str(path)


def _read(path: str) -> Any:
    from meshpipeline.cad.repair_v4.ws_kernel import read_brep

    return read_brep(path)


def _count(shape: Any, kind: str) -> int:
    return len(shells._entities(shape, kind))


def _valid(shape: Any) -> bool:
    from OCP.BRepCheck import BRepCheck_Analyzer

    return bool(BRepCheck_Analyzer(shape).IsValid())


def _free_edges(shape: Any) -> int:
    """Independent of the op's own count: ShapeAnalysis_FreeBounds over the result."""
    return shells._free_edge_count(shape)


def _inconsistent_orientation(shape: Any) -> bool:
    """ShapeAnalysis_Shell: does some edge have its two faces agreeing on its direction?"""
    from OCP.ShapeAnalysis import ShapeAnalysis_Shell

    analysis = ShapeAnalysis_Shell()
    analysis.LoadShells(shape)
    return bool(analysis.CheckOrientedShells(shape, True, False))


def _volume(shape: Any) -> float:
    return shells._volume(shape)


def _bound_of(shape: Any) -> float:
    return shells._bound(shape)[1]


def _samples(shape: Any) -> list[Any]:
    """Vertices plus a 5x5 grid of points on every face: what sampled deviation measures."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepTools import BRepTools
    from OCP.TopAbs import TopAbs_VERTEX
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.TopTools import TopTools_IndexedMapOfShape

    vertices = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_VERTEX, vertices)
    points = [BRep_Tool.Pnt_s(TopoDS.Vertex_s(vertices.FindKey(i)))
              for i in range(1, vertices.Extent() + 1)]
    for face in shells._entities(shape, "face"):
        u0, u1, v0, v1 = BRepTools.UVBounds_s(TopoDS.Face_s(face))
        surf = BRepAdaptor_Surface(TopoDS.Face_s(face))
        for i in range(5):
            for j in range(5):
                points.append(surf.Value(u0 + (u1 - u0) * i / 4, v0 + (v1 - v0) * j / 4))
    return points


def _one_sided(points: list[Any], other: Any) -> float:
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    worst = 0.0
    for p in points:
        dist = BRepExtrema_DistShapeShape(BRepBuilderAPI_MakeVertex(p).Vertex(), other)
        dist.Perform()
        worst = max(worst, dist.Value())
    return worst


def _deviation(a: Any, b: Any) -> float:
    return max(_one_sided(_samples(a), b), _one_sided(_samples(b), a))


# --- fixtures: damaged parts ---------------------------------------------------------------------

def _box() -> Any:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    return BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape()


def _loose_faces(gap: float) -> Any:
    """A box shelled into 6 loose faces, each pushed `gap` mm out from the centre."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.BRepGProp import BRepGProp
    from OCP.gp import gp_Pnt, gp_Trsf, gp_Vec
    from OCP.GProp import GProp_GProps
    from OCP.TopoDS import TopoDS_Compound

    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    for face in shells._entities(_box(), "face"):
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        away = gp_Vec(gp_Pnt(5, 5, 5), props.CentreOfMass())
        away.Normalize()
        away.Multiply(gap / 2)
        move = gp_Trsf()
        move.SetTranslation(away)
        builder.Add(compound, BRepBuilderAPI_Transform(face, move, True).Shape())
    return compound


def _ids(ws: Workspace, prefix: str) -> list[str]:
    return sorted(i for i in ws.ids if i.startswith(prefix))


# --- sew + make_volume ---------------------------------------------------------------------------

def test_sew_then_make_volume_turns_six_loose_faces_into_a_valid_solid(
    pool: WorkerPool, tmp_path: Path,
) -> None:
    loose = _loose_faces(1e-4)
    path = _write(loose, tmp_path / "loose.brep")
    ws = Workspace.from_brep(pool, path)
    faces_before, edges_before = _ids(ws, "F"), _ids(ws, "E")
    assert len(faces_before) == 6 and _free_edges(loose) > 0
    bound = _bound_of(loose)

    # ladder rungs 2e-5 and 5e-5 are tighter than the 1e-4 gaps: they must be tried and must fail,
    # and the op must stop at the first rung that closes the shell rather than the loosest
    sewn, sew_diff = OPS["sew"].apply(
        pool, ws, [], {"tol_ladder": [5e-4, 2e-5, 1e-4, 5e-5]}, timeout_s=TIMEOUT)
    tol_used = sew_diff.params["tol_used"]
    assert tol_used == 1e-4, sew_diff.params["tried"]
    assert [t["tol"] for t in sew_diff.params["tried"]] == [2e-5, 5e-5, 1e-4]  # tightest first, stops
    assert sew_diff.params["closed"] and sew_diff.params["free_edges_after"] == 0
    shell = _read(sewn.brep_path)
    assert _free_edges(shell) == 0 and _count(shell, "face") == 6
    assert _valid(shell)

    # identity under sewing: every face was rebuilt (new TShape) but Sewing's own mapping carries
    # it, so each face keeps its id; the 24 loose edges merge pairwise into 12 (ids retire/mint)
    assert _ids(sewn, "F") == faces_before
    assert all(sew_diff.modified[f] == [f] for f in faces_before)
    assert len(_ids(sewn, "E")) == 12 and not set(_ids(sewn, "E")) & set(edges_before)
    assert sew_diff.declared_removed == [] and sew_diff.declared_added == []

    solid_ws, vol_diff = OPS["make_volume"].apply(pool, sewn, [], {}, timeout_s=TIMEOUT)
    result = _read(solid_ws.brep_path)
    assert _count(result, "solid") == 1 and _free_edges(result) == 0
    assert _valid(result) and _volume(result) > 0
    assert _volume(result) == pytest.approx(1000.0, abs=1.0)
    assert _ids(solid_ws, "B") and _ids(solid_ws, "F") == faces_before, vol_diff
    # fidelity stand-in: the healed part stays within the bound of the damaged one
    assert _deviation(result, loose) <= bound
    assert tol_used <= bound


def test_sew_with_a_ladder_too_tight_for_the_gaps_declines_rather_than_going_loose(
    pool: WorkerPool, tmp_path: Path,
) -> None:
    ws = Workspace.from_brep(pool, _write(_loose_faces(1e-3), tmp_path / "wide.brep"))
    # the gaps (1e-3) are over the bound (~0.0173 is fine, but 1e-3 > every rung offered): no rung
    # closes the shell and none improves on the input, so the op declines instead of sewing loosely
    with pytest.raises(shells.OpDeclined):
        OPS["sew"].apply(pool, ws, [], {"tol_ladder": [2e-5, 1e-4]}, timeout_s=TIMEOUT)


def test_sew_declines_only_rungs_below_the_numerical_floor(pool: WorkerPool, tmp_path: Path) -> None:
    # B12: a rung over the old fidelity bound (0.05 mm) is accepted; a rung below 1e-6 x diagonal is still declined
    ws = Workspace.from_brep(pool, _write(_loose_faces(1e-4), tmp_path / "loose.brep"))
    try:
        OPS["sew"].apply(pool, ws, [], {"tol_ladder": [1e-4, 0.05]}, timeout_s=TIMEOUT)
    except shells.OpDeclined as why:
        assert "tol_ladder" not in why.reason, why.reason
    with pytest.raises(shells.OpDeclined, match="tol_ladder"):
        OPS["sew"].apply(pool, ws, [], {"tol_ladder": [1e-12]}, timeout_s=TIMEOUT)


def test_sew_keeps_the_ids_of_faces_it_leaves_alone(pool: WorkerPool, tmp_path: Path) -> None:
    """Only the top face is off by 8e-5; the five others need no change, so sewing may keep them as
    the same TShape. Either way every face keeps its id (Modified 1:1 or untouched)."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Trsf, gp_Vec
    from OCP.TopoDS import TopoDS_Compound

    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    for face in shells._entities(_box(), "face"):
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib

        box = Bnd_Box()
        BRepBndLib.Add_s(face, box)
        if abs(box.Get()[2] - 10.0) < 1e-6 and abs(box.Get()[5] - 10.0) < 1e-6:
            move = gp_Trsf()
            move.SetTranslation(gp_Vec(0, 0, 8e-5))
            face = BRepBuilderAPI_Transform(face, move, True).Shape()
        builder.Add(compound, face)
    assert _free_edges(compound) > 0  # the displaced face really is open (the box tolerance is ~1e-7)
    ws = Workspace.from_brep(pool, _write(compound, tmp_path / "one_off.brep"))
    sewn, diff = OPS["sew"].apply(pool, ws, [], {"tol_ladder": [2e-5, 1e-4]}, timeout_s=TIMEOUT)
    assert _ids(sewn, "F") == _ids(ws, "F")
    assert _free_edges(_read(sewn.brep_path)) == 0 and diff.removed == []


# --- orient_and_close ----------------------------------------------------------------------------

def _inside_out_solids() -> dict[str, Any]:
    from OCP.BRep import BRep_Builder
    from OCP.TopAbs import TopAbs_SHELL
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS, TopoDS_Solid

    box = _box()
    shell = TopoDS.Shell_s(TopExp_Explorer(box, TopAbs_SHELL).Current())
    from_reversed_shell = TopoDS_Solid()
    builder = BRep_Builder()
    builder.MakeSolid(from_reversed_shell)
    builder.Add(from_reversed_shell, shell.Reversed())
    return {"solid_reversed": shells._entities(box, "solid")[0].Reversed(),
            "shell_reversed": from_reversed_shell}


@pytest.mark.parametrize("variant", ["solid_reversed", "shell_reversed"])
def test_orient_and_close_makes_an_inside_out_solid_positive(
    pool: WorkerPool, tmp_path: Path, variant: str,
) -> None:
    inside_out = _inside_out_solids()[variant]
    assert _volume(inside_out) < 0  # the defect, measured
    ws = Workspace.from_brep(pool, _write(inside_out, tmp_path / f"{variant}.brep"))
    assert len(_ids(ws, "B")) == 1

    after, diff = OPS["orient_and_close"].apply(pool, ws, [], {}, timeout_s=TIMEOUT)
    result = _read(after.brep_path)
    assert _volume(result) == pytest.approx(1000.0, abs=1e-6)
    assert _valid(result) and _free_edges(result) == 0 and _count(result, "solid") == 1
    # orientation flips keep the TShapes, so identity is untouched: nothing removed or replaced
    assert after.ids == ws.ids and diff.removed == [] and diff.declared_removed == []
    assert _deviation(result, inside_out) <= _bound_of(result)


def test_orient_and_close_leaves_a_positive_solid_alone(pool: WorkerPool, tmp_path: Path) -> None:
    ws = Workspace.from_brep(pool, _write(_box(), tmp_path / "ok.brep"))
    after, diff = OPS["orient_and_close"].apply(pool, ws, [], {}, timeout_s=TIMEOUT)
    assert _volume(_read(after.brep_path)) == pytest.approx(1000.0, abs=1e-6)
    assert after.ids == ws.ids and diff.removed == []


# --- remove_small --------------------------------------------------------------------------------

def _chamfered_box(width: float) -> Any:
    """A 100 mm box with one edge chamfered so the chamfer face is `width` wide and 100 long."""
    from OCP.BRepFilletAPI import BRepFilletAPI_MakeChamfer
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.TopoDS import TopoDS

    box = BRepPrimAPI_MakeBox(100.0, 100.0, 100.0).Shape()
    chamfer = BRepFilletAPI_MakeChamfer(box)
    chamfer.Add(width / 2**0.5, TopoDS.Edge_s(shells._entities(box, "edge")[0]))
    chamfer.Build()
    return chamfer.Shape()


def _id_of_narrowest_face(ws: Workspace) -> str:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    areas = []
    for index, face in enumerate(shells._entities(_read(ws.brep_path), "face"), start=1):
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        areas.append((props.Mass(), index))
    _, index = min(areas)
    (sid,) = [i for i, n in ws.indices.items() if n == index and i.startswith("F")]
    return sid


def test_remove_small_removes_a_001mm_sliver_face_and_declares_it(
    pool: WorkerPool, tmp_path: Path,
) -> None:
    damaged = _chamfered_box(0.01)
    ws = Workspace.from_brep(pool, _write(damaged, tmp_path / "sliver.brep"))
    sliver = _id_of_narrowest_face(ws)
    others = [f for f in _ids(ws, "F") if f != sliver]
    assert len(others) == 6 and _volume(damaged) < 1e6
    bound = _bound_of(damaged)
    assert bound == pytest.approx(0.1)

    after, diff = OPS["remove_small"].apply(pool, ws, [sliver], {"max_width_mm": 0.05},
                                            timeout_s=TIMEOUT)
    result = _read(after.brep_path)
    assert _count(result, "face") == 6 and sliver not in after.ids
    assert _valid(result) and _volume(result) > 0 and _free_edges(result) == 0
    # the removal is DECLARED (exempt from fidelity but counted), and is exactly what the kernel's
    # own history reports removed - the sliver and nothing larger
    assert sliver in diff.declared_removed and sliver in diff.removed
    assert diff.params["small_faces_remaining"] == 0
    assert set(diff.declared_removed) == set(diff.removed)
    assert {i for i in diff.declared_removed if i.startswith("F")} == {sliver}
    # every other face keeps its id (Defeaturing reports each as Modified 1:1)
    assert _ids(after, "F") == sorted(others)
    # faithful: removing a 0.01 mm sliver moves no surface by more than the bound, in both
    # directions, even though the declared sliver face itself is gone from the result
    assert _deviation(result, damaged) <= bound
    assert _volume(result) == pytest.approx(1e6, rel=1e-6)


def test_remove_small_refuses_an_entity_wider_than_max_width(pool: WorkerPool, tmp_path: Path) -> None:
    ws = Workspace.from_brep(pool, _write(_chamfered_box(0.01), tmp_path / "sliver.brep"))
    big = next(f for f in _ids(ws, "F") if f != _id_of_narrowest_face(ws))
    with pytest.raises(shells.OpFailed, match="not small"):
        OPS["remove_small"].apply(pool, ws, [big], {"max_width_mm": 0.05}, timeout_s=TIMEOUT)


def test_remove_small_accepts_a_width_over_the_old_fidelity_bound(pool: WorkerPool, tmp_path: Path) -> None:
    """B12: defeaturing is allowed; a 0.5 mm width (far over the bound) is no longer refused as invalid params."""
    ws = Workspace.from_brep(pool, _write(_chamfered_box(0.01), tmp_path / "sliver.brep"))
    try:
        OPS["remove_small"].apply(pool, ws, [_id_of_narrowest_face(ws)], {"max_width_mm": 0.5},
                                  timeout_s=TIMEOUT)
    except shells.OpDeclined as why:
        assert "max_width_mm" not in why.reason, why.reason


def test_remove_small_heals_a_sliver_in_a_bare_shell_with_fix_small_face(
    pool: WorkerPool, tmp_path: Path,
) -> None:
    """No solid to defeature around (Defeaturing declines a bare shell), so the op falls back to
    ShapeFix_FixSmallFace; the removal is still declared and the shell is still closed."""
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Shell

    damaged = TopoDS_Shell()
    builder = BRep_Builder()
    builder.MakeShell(damaged)
    for face in shells._entities(_chamfered_box(0.01), "face"):
        builder.Add(damaged, face)
    ws = Workspace.from_brep(pool, _write(damaged, tmp_path / "shell.brep"))
    sliver = _id_of_narrowest_face(ws)

    after, diff = OPS["remove_small"].apply(pool, ws, [sliver], {"max_width_mm": 0.05},
                                            timeout_s=TIMEOUT)
    result = _read(after.brep_path)
    assert diff.params["via"] == ["fix_small_face"]
    assert _count(result, "face") == 6 and _valid(result) and _free_edges(result) == 0
    assert sliver in diff.declared_removed and set(diff.declared_removed) == set(diff.removed)
    assert _deviation(result, damaged) <= _bound_of(damaged)


def test_remove_small_collapses_a_tiny_edge_and_declares_it(pool: WorkerPool, tmp_path: Path) -> None:
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.gp import gp_Pnt, gp_Vec
    from OCP.GProp import GProp_GProps

    polygon = BRepBuilderAPI_MakePolygon()
    for x, y in [(0, 0), (100, 0), (100, 100), (50.02, 100), (50, 100.02), (0, 100)]:
        polygon.Add(gp_Pnt(x, y, 0))
    polygon.Close()
    damaged = BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(polygon.Wire()).Face(),
                                    gp_Vec(0, 0, 100)).Shape()
    ws = Workspace.from_brep(pool, _write(damaged, tmp_path / "prism.brep"))
    tiny = []
    for index, edge in enumerate(shells._entities(damaged, "edge"), start=1):
        props = GProp_GProps()
        BRepGProp.LinearProperties_s(edge, props)
        if props.Mass() < 0.05:
            tiny += [i for i, n in ws.indices.items() if n == index and i.startswith("E")]
    assert len(tiny) == 2

    after, diff = OPS["remove_small"].apply(pool, ws, tiny, {"max_width_mm": 0.05}, timeout_s=TIMEOUT)
    result = _read(after.brep_path)
    assert diff.params["via"] == ["fix_small_edges"]
    # the zero-width strip face is still there and the op says so, rather than reporting clean
    assert diff.params["small_faces_remaining"] >= 1
    assert sorted(diff.declared_removed) == sorted(tiny) and set(diff.removed) == set(tiny)
    assert _valid(result) and _free_edges(result) == 0 and _volume(result) > 0
    assert _count(result, "solid") == 1
    assert _deviation(result, damaged) <= _bound_of(damaged)


def test_orient_and_close_fixes_a_bare_shell_with_one_flipped_face(pool: WorkerPool, tmp_path: Path) -> None:
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Shell

    shell = TopoDS_Shell()
    builder = BRep_Builder()
    builder.MakeShell(shell)
    for i, face in enumerate(shells._entities(_box(), "face")):
        builder.Add(shell, face.Reversed() if i == 2 else face)
    assert _inconsistent_orientation(shell)  # the defect, measured
    ws = Workspace.from_brep(pool, _write(shell, tmp_path / "flipped.brep"))

    after, diff = OPS["orient_and_close"].apply(pool, ws, [], {}, timeout_s=TIMEOUT)
    result = _read(after.brep_path)
    assert not _inconsistent_orientation(result) and _valid(result) and _free_edges(result) == 0
    assert _count(result, "face") == 6 and _ids(after, "F") == _ids(ws, "F") and diff.removed == []


# --- review fixes: sew guard, width guard, unfinished-work signal ----------------------------------

def test_sew_declines_on_a_closed_solid_rather_than_destroying_it(pool: WorkerPool, tmp_path: Path) -> None:
    ws = Workspace.from_brep(pool, _write(_box(), tmp_path / "solid.brep"))
    with pytest.raises(shells.OpDeclined):
        OPS["sew"].apply(pool, ws, [], {"tol_ladder": [2e-5, 1e-4]}, timeout_s=TIMEOUT)


def test_sew_declines_on_a_closed_shell_with_nothing_to_sew(pool: WorkerPool, tmp_path: Path) -> None:
    from OCP.TopAbs import TopAbs_SHELL
    from OCP.TopExp import TopExp_Explorer

    shell = TopExp_Explorer(_box(), TopAbs_SHELL).Current()
    ws = Workspace.from_brep(pool, _write(shell, tmp_path / "shell.brep"))
    with pytest.raises(shells.OpDeclined):
        OPS["sew"].apply(pool, ws, [], {"tol_ladder": [2e-5, 1e-4]}, timeout_s=TIMEOUT)


def _big_box_with_loose_face(face: Any, tmp_path: Path) -> tuple[Any, str]:
    from OCP.BRep import BRep_Builder
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.TopoDS import TopoDS_Compound

    compound = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(compound)
    builder.Add(compound, BRepPrimAPI_MakeBox(100.0, 100.0, 100.0).Shape())
    builder.Add(compound, face)
    return compound, _write(compound, tmp_path / "with_face.brep")


@pytest.mark.parametrize("shape_name", ["square", "disc"])
def test_remove_small_width_guard_does_not_pass_a_face_twice_max_width_across(
    pool: WorkerPool, tmp_path: Path, shape_name: str,
) -> None:
    """max_width_mm 0.05; a 0.06 mm square (old 2A/P measure: 0.03) and a 0.06 mm disc (old: 0.03)
    are wider than that and must be refused, not removed and declared exempt."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeWire
    from OCP.gp import gp_Ax2, gp_Circ, gp_Dir, gp_Pln, gp_Pnt

    if shape_name == "square":
        face = BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(200, 0, 0), gp_Dir(0, 0, 1)),
                                       0.0, 0.06, 0.0, 0.06).Face()
    else:
        circle = gp_Circ(gp_Ax2(gp_Pnt(200, 0, 0), gp_Dir(0, 0, 1)), 0.03)
        wire = BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(circle).Edge()).Wire()
        face = BRepBuilderAPI_MakeFace(wire).Face()
    assert shells._width(face, "face") >= 0.06 - 1e-9  # measured at (not under) its true width
    _, path = _big_box_with_loose_face(face, tmp_path)
    ws = Workspace.from_brep(pool, path)
    target = _id_of_narrowest_face(ws)
    with pytest.raises(shells.OpFailed, match="not small"):
        OPS["remove_small"].apply(pool, ws, [target], {"max_width_mm": 0.05}, timeout_s=TIMEOUT)
