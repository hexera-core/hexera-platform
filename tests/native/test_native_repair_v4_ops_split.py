# Responsibility: Prove on the REAL kernel that the v4 split ops add topology and move nothing:
#                 each op on its fixture (thin-gap tube, full cylinder, C0 B-spline, split box),
#                 zero area/volume deviation, untouched faces keeping their stable ids, a split
#                 face's id becoming a validate_call error, and `keep` protecting a split edge.
# Boundaries: the native tier. Schema bounds and target validation are unit-tested hermetically in
#             tests/unit/cad/repair_v4/test_ops_registry.py.
from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("OCP.ShapeUpgrade", reason="the CAD kernel is a native-tier dependency")

from meshpipeline.cad.repair_v4.ops import (
    REGISTRY,
    OpDeclined,
    OpFailed,
    run_kernel_op,
    validate_call,
)  # noqa: E402
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool  # noqa: E402
from meshpipeline.cad.repair_v4.workspace import Workspace, apply_history  # noqa: E402

TIMEOUT_S = 600


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch) -> Iterator[WorkerPool]:
    monkeypatch.setenv("HEXERA_TEST_KERNEL_FNS", "1")
    p = WorkerPool(1)
    try:
        yield p
    finally:
        p.close()


def _write(shape: Any, path: Path) -> str:
    from OCP.BRepTools import BRepTools

    assert BRepTools.Write_s(shape, str(path))
    return str(path)


def _read(path: str) -> Any:
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape

    shape = TopoDS_Shape()
    assert BRepTools.Read_s(shape, path, BRep_Builder())
    return shape


def _indexed(shape: Any, kind: Any) -> Any:
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    out = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, kind, out)
    return out


def _count(path: str, kind: Any) -> int:
    return int(_indexed(_read(path), kind).Extent())


def _measure(path: str) -> tuple[float, float]:
    """(surface area, volume) of the shape at `path`."""
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    shape = _read(path)
    area, volume = GProp_GProps(), GProp_GProps()
    # Adaptive integration: the default Gauss rule is inexact across a C0 crease (0.6% off on the
    # B-spline fixture, whole vs. split), which is integration noise, not deviation.
    BRepGProp.SurfaceProperties_s(shape, area, 1e-9)
    BRepGProp.VolumeProperties_s(shape, volume, 1e-9)
    return float(area.Mass()), float(volume.Mass())


def _assert_no_deviation(before: str, after: str) -> None:
    from OCP.TopAbs import TopAbs_SOLID

    (a0, v0), (a1, v1) = _measure(before), _measure(after)
    assert abs(a1 - a0) <= 1e-6 * a0, (a0, a1)
    if _count(before, TopAbs_SOLID):  # a lone open face has no volume worth comparing
        assert abs(v1 - v0) <= 1e-6 * abs(v0) + 1e-9, (v0, v1)


def _id_where(ws: Workspace, kind: Any, prefix: str, pred: Callable[[Any], bool]) -> str:
    """The single stable id of the `prefix` entities of `ws`'s shape that satisfy `pred`."""
    indexed = _indexed(_read(ws.brep_path), kind)
    hits = [i for i in range(1, indexed.Extent() + 1) if pred(indexed.FindKey(i))]
    ids = [s for s, n in ws.indices.items() if s.startswith(prefix) and n in hits]
    assert len(ids) == len(hits) >= 1, (ids, hits)
    return sorted(ids)[0] if len(ids) == 1 else ",".join(sorted(ids))


def _surface_name(face: Any) -> str:
    from OCP.BRep import BRep_Tool
    from OCP.TopoDS import TopoDS

    return str(BRep_Tool.Surface_s(TopoDS.Face_s(face)).DynamicType().Name())


def _lateral(ws: Workspace) -> str:
    from OCP.TopAbs import TopAbs_FACE

    return _id_where(ws, TopAbs_FACE, "F", lambda f: _surface_name(f) == "Geom_CylindricalSurface")


def _cylinder_ws(pool: WorkerPool, tmp_path: Path) -> Workspace:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder

    return Workspace.from_brep(
        pool, _write(BRepPrimAPI_MakeCylinder(5.0, 10.0).Shape(), tmp_path / "cyl.brep"))


def _apply(pool: WorkerPool, name: str, ws: Workspace, targets: list[str], params: dict) -> Any:
    assert validate_call(name, targets, params, ws) == []
    return REGISTRY[name].apply(pool, ws, targets, params, timeout_s=TIMEOUT_S)


# --- split_angle / split_closed -----------------------------------------------------------------


def test_split_angle_splits_the_full_cylinder_and_moves_nothing(pool: WorkerPool,
                                                                tmp_path: Path) -> None:
    from OCP.TopAbs import TopAbs_FACE

    ws = _cylinder_ws(pool, tmp_path)
    lateral = _lateral(ws)
    caps = sorted(f for f in ws.ids if f.startswith("F") and f != lateral)
    new, diff = _apply(pool, "split_angle", ws, [lateral], {"max_deg": 90})
    assert _count(ws.brep_path, TopAbs_FACE) == 3
    assert _count(new.brep_path, TopAbs_FACE) == 3 + 3  # one lateral -> four quarters
    assert len(diff.modified[lateral]) == 4 and lateral not in new.ids
    # the caps were only re-bounded: same ids, same shape
    for cap in caps:
        assert cap in new.ids and diff.modified[cap] == [cap]
    _assert_no_deviation(ws.brep_path, new.brep_path)
    assert ws.brep_path != new.brep_path and lateral in ws.ids  # the input workspace is untouched


def test_a_split_faces_old_id_is_then_a_structured_validate_call_error(pool: WorkerPool,
                                                                      tmp_path: Path) -> None:
    ws = _cylinder_ws(pool, tmp_path)
    lateral = _lateral(ws)
    new, _ = _apply(pool, "split_angle", ws, [lateral], {"max_deg": 90})
    errors = validate_call("split_angle", [lateral], {"max_deg": 90}, new)
    assert len(errors) == 1 and f"{lateral}: replaced by op 'split_angle'" in errors[0], errors
    with pytest.raises(ValueError, match="replaced by op 'split_angle'"):
        REGISTRY["split_angle"].apply(pool, new, [lateral], {"max_deg": 90}, timeout_s=TIMEOUT_S)


def test_split_angle_declines_when_the_face_is_already_narrow_enough(pool: WorkerPool,
                                                                    tmp_path: Path) -> None:
    ws = _cylinder_ws(pool, tmp_path)
    lateral = _lateral(ws)
    new, diff = _apply(pool, "split_angle", ws, [lateral], {"max_deg": 90})
    with pytest.raises(OpDeclined, match="no_change"):
        REGISTRY["split_angle"].apply(pool, new, diff.modified[lateral], {"max_deg": 180},
                                      timeout_s=TIMEOUT_S)


def test_split_closed_gives_the_periodic_face_a_seam_free_boundary(pool: WorkerPool,
                                                                  tmp_path: Path) -> None:
    from OCP.TopAbs import TopAbs_FACE

    ws = _cylinder_ws(pool, tmp_path)
    lateral = _lateral(ws)
    new, diff = _apply(pool, "split_closed", ws, [lateral], {})
    assert _count(new.brep_path, TopAbs_FACE) > _count(ws.brep_path, TopAbs_FACE)
    assert lateral not in new.ids and len(diff.modified[lateral]) >= 2
    _assert_no_deviation(ws.brep_path, new.brep_path)
    with pytest.raises(OpDeclined, match="no_change"):  # the halves are no longer closed
        REGISTRY["split_closed"].apply(pool, new, diff.modified[lateral], {}, timeout_s=TIMEOUT_S)


def test_split_closed_falls_back_to_the_closed_rim_of_a_cap(pool: WorkerPool,
                                                            tmp_path: Path) -> None:
    from OCP.TopAbs import TopAbs_EDGE

    ws = _cylinder_ws(pool, tmp_path)
    lateral = _lateral(ws)
    cap = sorted(f for f in ws.ids if f.startswith("F") and f != lateral)[0]
    new, diff = _apply(pool, "split_closed", ws, [cap], {})
    assert _count(new.brep_path, TopAbs_EDGE) > _count(ws.brep_path, TopAbs_EDGE)
    assert diff.modified or diff.generated
    _assert_no_deviation(ws.brep_path, new.brep_path)


# --- split_continuity ---------------------------------------------------------------------------


def _c0_bspline_face() -> Any:
    """A cubic B-spline patch with an interior u-knot of multiplicity 3: C0 across it."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.Geom import Geom_BSplineSurface
    from OCP.gp import gp_Pnt
    from OCP.TColgp import TColgp_Array2OfPnt
    from OCP.TColStd import TColStd_Array1OfInteger, TColStd_Array1OfReal

    poles = TColgp_Array2OfPnt(1, 7, 1, 2)
    for i in range(1, 8):
        for j in range(1, 3):
            poles.SetValue(i, j, gp_Pnt(i * 2.0, j * 10.0, 3.0 if i % 2 else 0.0))
    uk, vk = TColStd_Array1OfReal(1, 3), TColStd_Array1OfReal(1, 2)
    for i, v in enumerate((0.0, 0.5, 1.0), 1):
        uk.SetValue(i, v)
    vk.SetValue(1, 0.0)
    vk.SetValue(2, 1.0)
    um, vm = TColStd_Array1OfInteger(1, 3), TColStd_Array1OfInteger(1, 2)
    for i, m in enumerate((4, 3, 4), 1):
        um.SetValue(i, m)
    vm.SetValue(1, 2)
    vm.SetValue(2, 2)
    return BRepBuilderAPI_MakeFace(Geom_BSplineSurface(poles, uk, vk, um, vm, 3, 1), 1e-6).Face()


def test_split_continuity_cuts_a_c0_bspline_at_its_crease(pool: WorkerPool, tmp_path: Path) -> None:
    from OCP.TopAbs import TopAbs_FACE

    ws = Workspace.from_brep(pool, _write(_c0_bspline_face(), tmp_path / "c0.brep"))
    (face,) = [s for s in ws.ids if s.startswith("F")]
    new, diff = _apply(pool, "split_continuity", ws, [face], {"order": 1})
    assert _count(ws.brep_path, TopAbs_FACE) == 1 and _count(new.brep_path, TopAbs_FACE) == 2
    assert face not in new.ids and len(diff.modified[face]) == 2
    _assert_no_deviation(ws.brep_path, new.brep_path)
    with pytest.raises(OpDeclined, match="no_change"):  # each half is C1 now
        REGISTRY["split_continuity"].apply(pool, new, diff.modified[face], {"order": 1},
                                           timeout_s=TIMEOUT_S)


# --- unify_same_domain --------------------------------------------------------------------------


def _split_top(pool: WorkerPool, ws: Workspace, y: float) -> Workspace:
    """Split the box's top face with a straight edge at `y`, through the kernel's test op."""
    out = str(Path(ws.brep_path).parent / f"split-{y}.brep")
    result = pool.call("ws_split_face_by_edge", {
        "brep_path": ws.brep_path, "out_path": out, "quantum": ws.quantum,
        "p1": [0.0, y, 10.0], "p2": [10.0, y, 10.0]}, timeout_s=TIMEOUT_S)
    assert result.ok and result.value is not None, result.error
    return apply_history(ws, result.value)


def _edge_at_y(ws: Workspace, y: float) -> str:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.TopAbs import TopAbs_EDGE

    def at_y(edge: Any) -> bool:
        box = Bnd_Box()
        BRepBndLib.Add_s(edge, box)
        x0, y0, z0, x1, y1, z1 = box.Get()
        return abs(y0 - y) < 1e-6 and abs(y1 - y) < 1e-6 and abs(z0 - 10) < 1e-6 \
            and abs(z1 - 10) < 1e-6 and x1 - x0 > 9

    return _id_where(ws, TopAbs_EDGE, "E", at_y)


def test_unify_same_domain_merges_split_faces_unless_the_edge_is_kept(pool: WorkerPool,
                                                                     tmp_path: Path) -> None:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.TopAbs import TopAbs_FACE

    ws0 = Workspace.from_brep(
        pool, _write(BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape(), tmp_path / "box.brep"))
    ws = _split_top(pool, _split_top(pool, ws0, 3.0), 7.0)
    assert _count(ws.brep_path, TopAbs_FACE) == 8  # the top is three strips
    kept, free = _edge_at_y(ws, 3.0), _edge_at_y(ws, 7.0)

    merged, _ = _apply(pool, "unify_same_domain", ws, [], {"scope": "all", "keep": []})
    assert _count(merged.brep_path, TopAbs_FACE) == 6
    assert _count(merged.brep_path, _edge_kind()) == 12

    protected, diff = _apply(pool, "unify_same_domain", ws, [], {"scope": "all", "keep": [kept]})
    assert _count(protected.brep_path, TopAbs_FACE) == 7  # the strip beyond y=7 merged, y=3 held
    assert kept in protected.ids and free not in protected.ids and free in diff.removed
    _assert_no_deviation(ws.brep_path, protected.brep_path)

    # keeping BOTH leaves nothing to merge: a decline, not a silent no-op
    with pytest.raises(OpDeclined, match="no_change"):
        REGISTRY["unify_same_domain"].apply(pool, ws, [], {"scope": "all", "keep": [kept, free]},
                                            timeout_s=TIMEOUT_S)


def _edge_kind() -> Any:
    from OCP.TopAbs import TopAbs_EDGE

    return TopAbs_EDGE


def test_unify_same_domain_scope_targets_merges_only_among_the_targets(pool: WorkerPool,
                                                                     tmp_path: Path) -> None:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.TopAbs import TopAbs_FACE

    ws0 = Workspace.from_brep(
        pool, _write(BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape(), tmp_path / "box.brep"))
    ws = _split_top(pool, _split_top(pool, ws0, 3.0), 7.0)
    faces = _indexed(_read(ws.brep_path), TopAbs_FACE)

    def on_top(sid: str) -> bool:
        box = Bnd_Box()
        BRepBndLib.Add_s(faces.FindKey(ws.indices[sid]), box)
        return bool(abs(box.Get()[2] - 10) < 1e-6 and abs(box.Get()[5] - 10) < 1e-6)

    strips = sorted(sid for sid in ws.ids if sid.startswith("F") and on_top(sid))
    assert len(strips) == 3
    merged, _ = _apply(pool, "unify_same_domain", ws, strips, {"scope": "targets", "keep": []})
    assert _count(merged.brep_path, TopAbs_FACE) == 6
    with pytest.raises(OpDeclined, match="no_change"):  # one strip alone has nothing to merge with
        REGISTRY["unify_same_domain"].apply(pool, ws, strips[:1], {"scope": "targets", "keep": []},
                                            timeout_s=TIMEOUT_S)


def test_unify_same_domain_scope_rules_refuse_the_call(pool: WorkerPool, tmp_path: Path) -> None:
    ws = _cylinder_ws(pool, tmp_path)
    with pytest.raises(ValueError, match="needs target faces"):
        REGISTRY["unify_same_domain"].apply(pool, ws, [], {"scope": "targets", "keep": []},
                                            timeout_s=TIMEOUT_S)
    with pytest.raises(ValueError, match="takes no targets"):
        REGISTRY["unify_same_domain"].apply(pool, ws, [_lateral(ws)], {"scope": "all", "keep": []},
                                            timeout_s=TIMEOUT_S)


# --- split_by_proximity -------------------------------------------------------------------------

#: A 50 mm tube, 20 mm tall, with a 0.02 mm wall (the legacy gap-split fixture). The probe's mesh
#: size is diagonal / 40 = 3.57 mm, whose chord sags 0.032 mm inside the outer cylinder: wider than
#: the wall, so the default probe refuses the part.
RADIUS_MM, WALL_MM, HEIGHT_MM = 50.0, 0.02, 20.0


def _tube() -> Any:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder

    return BRepAlgoAPI_Cut(BRepPrimAPI_MakeCylinder(RADIUS_MM, HEIGHT_MM).Shape(),
                           BRepPrimAPI_MakeCylinder(RADIUS_MM - WALL_MM, HEIGHT_MM).Shape()).Shape()


def _meshes(brep_path: str, tmp_path: Path, name: str) -> tuple[bool, str]:
    """The cheap verdict: does the default gmsh probe mesh the part (through a STEP round trip)?"""
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    from meshpipeline.cad.repair import mesh_probe
    from meshpipeline.cad.repair.localize import _diagonal

    shape = _read(brep_path)
    step = tmp_path / f"{name}.step"
    writer = STEPControl_Writer()
    writer.Transfer(shape, STEPControl_AsIs)
    writer.Write(str(step))
    result = mesh_probe.probe_file(step, diagonal_mm=_diagonal(shape), budget_s=300)
    return bool(result.meshed), str(getattr(result.failure, "kind", "") or "")


def test_split_by_proximity_turns_the_refused_thin_wall_into_a_meshable_part(
        pool: WorkerPool, tmp_path: Path) -> None:
    pytest.importorskip("gmsh", reason="the mesh probe is a native-tier dependency")
    from OCP.TopAbs import TopAbs_FACE

    from meshpipeline.cad.repair import mesh_probe

    ws = Workspace.from_brep(pool, _write(_tube(), tmp_path / "tube.brep"))
    assert _meshes(ws.brep_path, tmp_path, "before") == (False, mesh_probe.EDGE_THROUGH_FACE)

    def radius(face: Any) -> float:
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        from OCP.GeomAbs import GeomAbs_Cylinder
        from OCP.TopoDS import TopoDS

        surface = BRepAdaptor_Surface(TopoDS.Face_s(face))
        return float(surface.Cylinder().Radius()) if surface.GetType() == GeomAbs_Cylinder else -1.0

    outer = _id_where(ws, TopAbs_FACE, "F", lambda f: abs(radius(f) - RADIUS_MM) < 1e-6)
    inner = _id_where(ws, TopAbs_FACE, "F", lambda f: abs(radius(f) - (RADIUS_MM - WALL_MM)) < 1e-6)
    new, diff = _apply(pool, "split_by_proximity", ws, [],
                       {"face_pairs": [[outer, inner]], "target_ratio": 0.25})

    assert _count(new.brep_path, TopAbs_FACE) > _count(ws.brep_path, TopAbs_FACE)
    assert len(diff.modified[outer]) > 1 and len(diff.modified[inner]) > 1
    _assert_no_deviation(ws.brep_path, new.brep_path)  # topology only: no surface moved
    assert _meshes(new.brep_path, tmp_path, "after") == (True, "")


# --- runtime guard and worker failure -----------------------------------------------------------


def test_guard_declines_only_a_changed_solid_count_never_moved_geometry(
        tmp_path: Path) -> None:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder

    from meshpipeline.cad.repair_v4.ops.split import guard

    box = BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape()
    assert guard(box, box) is None
    # B12: moved geometry is reported elsewhere (layer 3), never refused by the guard
    assert guard(box, BRepPrimAPI_MakeBox(10.0, 10.0, 10.5).Shape()) is None
    assert guard(box, BRepPrimAPI_MakeCylinder(5.0, 10.0).Shape()) is None
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    two = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(two)
    builder.Add(two, box)
    builder.Add(two, BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape())
    assert "solid count" in str(guard(box, two))


def test_a_timed_out_op_raises_op_failed_and_leaves_no_partial_file(pool: WorkerPool,
                                                                   tmp_path: Path) -> None:
    ws = _cylinder_ws(pool, tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())
    with pytest.raises(OpFailed) as caught:
        run_kernel_op(pool, ws, kernel_fn="op_test_write_then_hang", op="hang", payload={},
                      timeout_s=5)
    assert caught.value.status == "timeout"
    assert sorted(p.name for p in tmp_path.iterdir()) == before  # the partial output was removed


def test_a_crashed_or_unknown_kernel_call_raises_op_failed(pool: WorkerPool,
                                                           tmp_path: Path) -> None:
    ws = _cylinder_ws(pool, tmp_path)
    with pytest.raises(OpFailed) as crashed:
        run_kernel_op(pool, ws, kernel_fn="abort", op="abort", payload={}, timeout_s=60)
    assert crashed.value.status == "crashed"
    with pytest.raises(OpFailed) as unknown:
        run_kernel_op(pool, ws, kernel_fn="no_such_fn", op="x", payload={}, timeout_s=60)
    assert unknown.value.status == "error" and "no_such_fn" in unknown.value.message
