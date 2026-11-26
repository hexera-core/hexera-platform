# Responsibility: Verify layer 3 of the v4 checker (faithful) against the real kernel: the bound is
#                 millimetres whatever the file's unit, declared added/removed faces are exempt in
#                 exactly one direction each, fills are size-limited, and the assembled Verdict is
#                 clean only when every layer is.
# Boundaries: the native tier - the only place OpenCASCADE exists. The kernel under test runs in a
#             WorkerPool process; this module touches OCP only to author fixtures. Layer 2 is a
#             fake PanelMember: real meshers belong to Tasks 3/4.
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

pytest.importorskip("OCP.XCAFDoc", reason="the CAD kernel is a native-tier dependency")

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_Sewing, BRepBuilderAPI_Transform
from OCP.BRepGProp import BRepGProp
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.BRepTools import BRepTools
from OCP.gp import gp_Dir, gp_Pln, gp_Pnt, gp_Trsf, gp_Vec
from OCP.GProp import GProp_GProps
from OCP.IFSelect import IFSelect_RetDone
from OCP.Interface import Interface_Static
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
from OCP.TopAbs import TopAbs_FACE
from OCP.TopExp import TopExp
from OCP.TopoDS import TopoDS_Shape
from OCP.TopTools import TopTools_IndexedMapOfShape

from meshpipeline.cad.repair_v4.verdict import Declared, check_faithful, verdict
from meshpipeline.cad.repair_v4.verdict.cache import DirStore, VerdictCache
from meshpipeline.cad.repair_v4.verdict.faithful import DeclaredIdError, check_faithful_steps
from meshpipeline.cad.repair_v4.verdict.valid import CheckerUnavailable
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool


@pytest.fixture(scope="module")
def pool() -> Iterator[WorkerPool]:
    p = WorkerPool(1, start_timeout_s=120)
    try:
        yield p
    finally:
        p.close()


def _box(size: float = 100.0, at: tuple[float, float, float] = (0.0, 0.0, 0.0)):
    return BRepPrimAPI_MakeBox(gp_Pnt(*at), size, size, size).Shape()


def _moved(shape, dx: float = 0.0, dy: float = 0.0, dz: float = 0.0):
    trsf = gp_Trsf()
    trsf.SetTranslation(gp_Vec(dx, dy, dz))
    return BRepBuilderAPI_Transform(shape, trsf, True).Shape()


def _scaled(shape, factor: float):
    trsf = gp_Trsf()
    trsf.SetScale(gp_Pnt(0, 0, 0), factor)
    return BRepBuilderAPI_Transform(shape, trsf, True).Shape()


def _write_step(shape, path: Path, unit: str = "MM") -> str:
    writer = STEPControl_Writer()
    assert Interface_Static.SetCVal_s("write.step.unit", unit)
    try:
        writer.Transfer(shape, STEPControl_AsIs)
        assert writer.Write(str(path)) == IFSelect_RetDone
    finally:
        Interface_Static.SetCVal_s("write.step.unit", "MM")
    return str(path)


def _write_brep(shape, path: Path) -> str:
    assert BRepTools.Write_s(shape, str(path))
    return str(path)


def _faces(shape) -> list:
    found = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, found)
    return [found.FindKey(i) for i in range(1, found.Extent() + 1)]


def _area(shape) -> float:
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, props)
    return props.Mass()


def _smallest_face_id(brep: str) -> str:
    """`F<k>` of the smallest face in the BREP as the worker will read it."""
    shape = TopoDS_Shape()
    from OCP.BRep import BRep_Builder

    assert BRepTools.Read_s(shape, brep, BRep_Builder())
    faces = _faces(shape)
    return f"F{min(range(len(faces)), key=lambda i: _area(faces[i])) + 1}"


def _shell_of_faces(faces) -> TopoDS_Shape:
    sewing = BRepBuilderAPI_Sewing(1e-6)
    for face in faces:
        sewing.Add(face)
    sewing.Perform()
    return sewing.SewedShape()


def _plate_with_open_hole(hole: float, *, patched: bool, size: float = 100.0):
    """A box surface whose top face has a square `hole` cut out (an open shell), optionally with a
    planar patch sewn into it. Returns (shape, patch_present)."""
    box = _box(size)
    top = max(_faces(box), key=lambda f: BRepGProp_centroid_z(f))
    lo = (size - hole) / 2
    cutter = BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(0, 0, size), gp_Dir(0, 0, 1)), lo, lo + hole, lo, lo + hole).Face()
    holed = BRepAlgoAPI_Cut(top, cutter).Shape()
    others = [f for f in _faces(box) if not f.IsSame(top)]
    parts = [*others, *_faces(holed)]
    if patched:
        parts.append(cutter)
    return _shell_of_faces(parts)


def _plate_with_two_open_holes(*, patched: bool, size: float = 100.0, hole: float = 2.0):
    box = _box(size)
    top = max(_faces(box), key=lambda f: BRepGProp_centroid_z(f))
    plane = gp_Pln(gp_Pnt(0, 0, size), gp_Dir(0, 0, 1))
    cutters = [BRepBuilderAPI_MakeFace(plane, lo, lo + hole, lo, lo + hole).Face() for lo in (5.0, size - 5.0 - hole)]
    holed = BRepAlgoAPI_Cut(top, cutters[0]).Shape()
    holed = BRepAlgoAPI_Cut(holed, cutters[1]).Shape()
    parts = [*[f for f in _faces(box) if not f.IsSame(top)], *_faces(holed)]
    if patched:
        parts.extend(cutters)
    return _shell_of_faces(parts)


def BRepGProp_centroid_z(face) -> float:
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    return props.CentreOfMass().Z()


def _check(pool: WorkerPool, original, repaired, tmp_path: Path, added=(), removed=()):
    o = _write_brep(original, tmp_path / "o.brep")
    r = _write_brep(repaired, tmp_path / "r.brep")
    added_ids = [_smallest_face_id(r)] if added else []
    removed_ids = [_smallest_face_id(o)] if removed else []
    return check_faithful(pool, o, r, declared_added=added_ids, declared_removed=removed_ids)


def test_a_box_translated_0_2_mm_on_a_100_mm_part_is_not_faithful(pool, tmp_path) -> None:
    box = _box()
    report = check_faithful(
        pool,
        _write_brep(box, tmp_path / "o.brep"),
        _write_brep(_moved(box, dx=0.2), tmp_path / "r.brep"),
        declared_added=[],
        declared_removed=[],
    )
    assert report.ok is False
    assert report.max_dev_mm == pytest.approx(0.2, abs=0.01)
    assert report.bound_mm == pytest.approx(0.1)
    assert report.reason == "deviation_exceeds_bound"


def test_a_box_translated_0_05_mm_is_faithful(pool, tmp_path) -> None:
    box = _box()
    report = check_faithful(
        pool,
        _write_brep(box, tmp_path / "o.brep"),
        _write_brep(_moved(box, dx=0.05), tmp_path / "r.brep"),
        declared_added=[],
        declared_removed=[],
    )
    assert report.ok is True, report.reasons
    assert report.max_dev_mm == pytest.approx(0.05, abs=0.01)


def test_the_bound_shrinks_with_the_diagonal_on_a_small_part(pool, tmp_path) -> None:
    small = _box(10.0)  # diagonal 17.3 mm -> bound 0.0173 mm, not 0.1
    report = check_faithful(
        pool,
        _write_brep(small, tmp_path / "o.brep"),
        _write_brep(_moved(small, dx=0.05), tmp_path / "r.brep"),
        declared_added=[],
        declared_removed=[],
    )
    assert report.bound_mm == pytest.approx(0.001 * 17.3205, rel=1e-3)
    assert report.ok is False


def test_inch_part_deviation_is_measured_in_millimetres(pool, tmp_path) -> None:
    inch_box = _box(254.0)  # 10 in
    original = _write_step(inch_box, tmp_path / "o.step", unit="INCH")
    inside = _write_step(_moved(inch_box, dx=0.003 * 25.4), tmp_path / "in.step", unit="INCH")
    outside = _write_step(_moved(inch_box, dx=0.005 * 25.4), tmp_path / "out.step", unit="INCH")

    ok = check_faithful_steps(pool, original, inside, declared_added=[], declared_removed=[])
    assert ok.max_dev_mm == pytest.approx(0.0762, abs=0.005)  # 0.003 in, reported in mm
    assert ok.bound_mm == pytest.approx(0.1)
    assert ok.ok is True, ok.reasons

    bad = check_faithful_steps(pool, original, outside, declared_added=[], declared_removed=[])
    assert bad.max_dev_mm == pytest.approx(0.127, abs=0.005)  # 0.005 in = 0.127 mm: over a 0.1 mm bound
    assert bad.ok is False  # a reader taking the number as inches would call 0.005 fine


def test_a_planar_fill_of_a_2_mm_hole_declared_added_is_one_sided_and_ok(pool, tmp_path) -> None:
    original = _plate_with_open_hole(2.0, patched=False)
    repaired = _plate_with_open_hole(2.0, patched=True)
    declared = _check(pool, original, repaired, tmp_path, added=True)
    assert declared.ok is True, (declared.reasons, declared.max_dev_mm)
    assert declared.max_dev_mm <= 0.01

    undeclared = _check(pool, original, repaired, tmp_path)
    assert undeclared.ok is False  # the patch is 1 mm from anything the original has
    assert undeclared.max_dev_mm == pytest.approx(1.0, abs=0.05)
    assert undeclared.reason == "deviation_exceeds_bound"


def test_a_fill_spanning_more_than_5_percent_of_the_diagonal_is_too_large(pool, tmp_path) -> None:
    original = _plate_with_open_hole(30.0, patched=False)  # a 42 mm loop on a 173 mm diagonal
    repaired = _plate_with_open_hole(30.0, patched=True)
    report = _check(pool, original, repaired, tmp_path, added=True)
    assert report.ok is False
    assert report.reason == "fill_too_large"
    assert report.reasons == ["fill_too_large"]  # nothing else is wrong: only the size


def _with_stray_face(shell, x: float = 10.0):
    """`shell` plus a 1 mm face floating 3 mm above the top: a sliver the repair will drop."""
    stray = BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(0, 0, 103), gp_Dir(0, 0, 1)), x, x + 1, 10, 11).Face()
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    comp = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(comp)
    builder.Add(comp, shell)
    builder.Add(comp, stray)
    return comp


def test_a_declared_removal_is_exempt_only_original_to_repaired(pool, tmp_path) -> None:
    clean = _plate_with_open_hole(2.0, patched=True)
    with_stray = _with_stray_face(clean)

    # The repair REMOVED the stray face: sampled original -> repaired it is 3 mm from anything.
    undeclared = _check(pool, with_stray, clean, tmp_path)
    assert undeclared.ok is False and undeclared.reason == "deviation_exceeds_bound"
    declared = _check(pool, with_stray, clean, tmp_path, removed=True)
    assert declared.ok is True, (declared.reasons, declared.max_dev_mm)

    # The repair dropped stray A (declared) but ADDED stray B elsewhere (undeclared): the removal
    # declaration is exempt original -> repaired only, so B is still measured repaired -> original.
    swapped = _check(pool, with_stray, _with_stray_face(clean, 60.0), tmp_path, removed=True)
    assert swapped.ok is False and swapped.reason == "deviation_exceeds_bound"
    # Declaring B ADDED exempts it in that direction (and A removed in the other).
    o = _write_brep(with_stray, tmp_path / "o2.brep")
    r = _write_brep(_with_stray_face(clean, 60.0), tmp_path / "r2.brep")
    both = check_faithful(pool, o, r, declared_added=[_smallest_face_id(r)], declared_removed=[_smallest_face_id(o)])
    assert both.ok is True, (both.reasons, both.max_dev_mm)


def test_two_small_fills_far_apart_are_not_one_large_fill(pool, tmp_path) -> None:
    """The span rule is per fill: two 2 mm fills at opposite ends of a 173 mm part are two small
    loops, not one 100 mm loop."""
    original = _plate_with_two_open_holes(patched=False)
    repaired = _plate_with_two_open_holes(patched=True)
    o = _write_brep(original, tmp_path / "o.brep")
    r = _write_brep(repaired, tmp_path / "r.brep")
    shape = TopoDS_Shape()
    from OCP.BRep import BRep_Builder

    assert BRepTools.Read_s(shape, r, BRep_Builder())
    faces = _faces(shape)
    small = [f"F{i + 1}" for i, f in enumerate(faces) if _area(f) < 10]
    assert len(small) == 2
    report = check_faithful(pool, o, r, declared_added=small, declared_removed=[])
    assert report.ok is True, (report.reasons, report.max_dev_mm)


def test_an_in_range_but_wrong_declared_id_is_refused(pool, tmp_path) -> None:
    """Declaring an UNCHANGED face as added would exempt it silently (and so could hide a real
    deviation); ids that name a face the other shape also has are refused as the caller's bug."""
    original = _plate_with_open_hole(2.0, patched=False)
    repaired = _plate_with_open_hole(2.0, patched=True)
    o = _write_brep(original, tmp_path / "o.brep")
    r = _write_brep(repaired, tmp_path / "r.brep")
    patch = int(_smallest_face_id(r)[1:])
    wrong = f"F{1 if patch != 1 else 2}"
    with pytest.raises(DeclaredIdError):
        check_faithful(pool, o, r, declared_added=[wrong], declared_removed=[])
    # the converse - a "removed" face the repaired shape still has - is what merging two coincident faces truthfully
    # declares (CRS v5 r2 fix I): it is MEASURED, not exempt, so it can hide nothing, and it is named
    report = check_faithful(pool, o, r, declared_added=[], declared_removed=["F1"])
    assert report.declared_measured == ["F1"]


def test_area_change_over_2_percent_fails(pool, tmp_path) -> None:
    box = _box()
    report = check_faithful(
        pool,
        _write_brep(box, tmp_path / "o.brep"),
        _write_brep(_scaled(box, 1.025), tmp_path / "r.brep"),
        declared_added=[],
        declared_removed=[],
    )
    assert report.ok is False
    assert "area_changed" in report.reasons and "volume_changed" in report.reasons
    assert report.area_ratio == pytest.approx(1.025**2, rel=1e-3)
    assert report.volume_ratio == pytest.approx(1.025**3, rel=1e-3)


def test_body_count_must_be_preserved(pool, tmp_path) -> None:
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    comp = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(comp)
    builder.Add(comp, _box(10.0))
    builder.Add(comp, _box(10.0, (50, 0, 0)))
    one = _box(10.0)
    report = check_faithful(
        pool,
        _write_brep(comp, tmp_path / "o.brep"),
        _write_brep(one, tmp_path / "r.brep"),
        declared_added=[],
        declared_removed=[],
    )
    assert (report.bodies_before, report.bodies_after) == (2, 1)
    assert "body_count_changed" in report.reasons and report.ok is False


def test_an_out_of_range_declared_id_is_the_callers_bug_not_infrastructure(pool, tmp_path) -> None:
    path = _write_brep(_box(), tmp_path / "b.brep")
    with pytest.raises(DeclaredIdError) as raised:
        check_faithful(pool, path, path, declared_added=["F999"], declared_removed=[])
    assert not isinstance(raised.value, CheckerUnavailable)


# ---- the assembled verdict, with a fake layer 2 ---------------------------------------------


@dataclass(frozen=True)
class _Result:
    name: str
    status: str
    min_sicn: float | None = 0.3
    located: list = field(default_factory=list)


@dataclass
class _Member:
    name: str = "fake"
    status: str = "pass"
    runs: int = 0
    config: dict = field(default_factory=dict)

    def run(self, pool, step_path, *, timeout_s):
        self.runs += 1
        return _Result(self.name, self.status)


def _run_panel(pool, step, members, *, timeout_s):
    return [m.run(pool, step, timeout_s=timeout_s) for m in members]


def test_the_bbox_ratio_is_measured_on_real_shapes(pool, tmp_path) -> None:
    """The number report.py's gaming flag reads: the repaired shape's extent over the original's."""
    original = _write_brep(_box(100.0), tmp_path / "o.brep")
    same = check_faithful(pool, original, original, declared_added=[], declared_removed=[])
    assert same.bbox_ratio == pytest.approx(1.0) and same.diag_after_mm == pytest.approx(same.bbox_ratio * 173.2, abs=1)
    half = check_faithful(pool, original, _write_brep(_box(50.0), tmp_path / "h.brep"),
                          declared_added=[], declared_removed=[])
    assert half.bbox_ratio == pytest.approx(0.5, abs=0.01)   # a part shrunk to half its extent
    assert half.volume_ratio == pytest.approx(0.125, abs=0.01)


def test_verdict_over_a_real_file_is_clean_by_valid_and_panel_alone(pool, tmp_path) -> None:
    box = _box()
    original = _write_step(box, tmp_path / "o.step")
    same = _write_step(box, tmp_path / "same.step")
    moved = _write_step(_moved(box, dx=0.2), tmp_path / "moved.step")

    good = verdict(pool, same, original, members=[_Member()], run_panel=_run_panel)
    assert good.valid.ok and good.faithful is not None and good.faithful.ok and good.clean is True

    # a real part moved 0.2 mm: layer 3 says unfaithful, and since the owner's 2026-11-24 decision it is CLEAN
    bad = verdict(pool, moved, original, members=[_Member()], run_panel=_run_panel)
    assert bad.valid.ok and bad.faithful is not None and bad.faithful.ok is False
    assert bad.clean is True

    unmeshable = verdict(pool, same, original, members=[_Member(status="fail")], run_panel=_run_panel)
    assert unmeshable.faithful is not None and unmeshable.faithful.ok and unmeshable.clean is False

    assert verdict(pool, same, original, declared=Declared(), members=[_Member()], run_panel=_run_panel).tier == "full"


def test_cache_means_the_second_verdict_does_not_mesh(pool, tmp_path) -> None:
    def factory(data: dict) -> _Result:
        return _Result(data["name"], data["status"], data["min_sicn"], data["located"])

    cache = VerdictCache(DirStore(tmp_path / "bucket"), result_factory=factory)
    step = _write_step(_box(), tmp_path / "p.step")
    member = _Member()
    first = verdict(pool, step, step, members=[member], run_panel=_run_panel, cache=cache)
    second = verdict(pool, step, step, members=[member], run_panel=_run_panel, cache=cache)
    assert member.runs == 1
    assert first.clean and second.clean


def _loose_box_faces():
    """A box as six loose faces: no solid, no shell."""
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    comp = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(comp)
    for face in _faces(_box()):
        builder.Add(comp, face)
    return comp


def _sewn_solid(faces, dz: float = 0.0):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid
    from OCP.TopAbs import TopAbs_SHELL
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    shell = _shell_of_faces(faces)
    explorer = TopExp_Explorer(shell, TopAbs_SHELL)
    solid = BRepBuilderAPI_MakeSolid(TopoDS.Shell_s(explorer.Current())).Solid()
    return _moved(solid, dz=dz) if dz else solid


def test_a_correct_heal_of_a_solidless_input_reads_clean_and_a_damaging_one_does_not(pool, tmp_path) -> None:
    """An unsewn box has no volume and no body. A sew that makes it a solid must be able to read
    clean (it used to fail volume_changed/body_count_changed whatever the repair did), while a
    heal that moves geometry, or leaves the result invalid, must still fail."""
    loose = _loose_box_faces()
    original = _write_step(loose, tmp_path / "loose.step")
    good = _write_step(_sewn_solid(_faces(loose)), tmp_path / "sewn.step")
    # The box is 100 mm on a side (diagonal 173 mm), so the bound is 0.1 mm and 0.5 mm is 5x over it.
    moved = _write_step(_sewn_solid(_faces(loose), dz=0.5), tmp_path / "moved.step")
    open_shell = _write_step(_shell_of_faces(_faces(loose)[:5]), tmp_path / "open.step")

    ok = verdict(pool, good, original, members=[_Member()], run_panel=_run_panel)
    assert ok.valid.ok and ok.faithful is not None
    assert ok.faithful.ok is True, (ok.faithful.reasons, ok.faithful.max_dev_mm)
    assert (ok.faithful.bodies_before, ok.faithful.bodies_after) == (6, 1)  # six loose faces read as shells
    assert ok.clean is True

    shifted = verdict(pool, moved, original, members=[_Member()], run_panel=_run_panel)
    assert shifted.valid.ok and shifted.faithful is not None
    assert shifted.faithful.reason == "deviation_exceeds_bound" and shifted.clean is True  # reported, not gating

    broken = verdict(pool, open_shell, original, members=[_Member()], run_panel=_run_panel)
    assert broken.valid.ok is False and broken.clean is False  # a dropped face: layer 1 says invalid
    # ...and independently of layer 1, layer 3 still holds the area line on a solid-less original
    # (5 of 6 faces = -16.7%), while volume/body count stay exempt there.
    faithful = check_faithful_steps(pool, original, open_shell, declared_added=[], declared_removed=[])
    assert faithful.ok is False and "area_changed" in faithful.reasons
    assert "volume_changed" not in faithful.reasons and "body_count_changed" not in faithful.reasons
    assert faithful.area_ratio == pytest.approx(5 / 6, rel=1e-3)


def test_a_zero_area_original_still_flags_area_changed(pool, tmp_path) -> None:
    """The original is one 100 mm edge (extent, but no area); the result has a 100 x 100 face. The
    area rule must fire from a zero baseline, and the report carries None, not a fake 1.0."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge

    edge = BRepBuilderAPI_MakeEdge(gp_Pnt(0, 0, 0), gp_Pnt(100, 0, 0)).Edge()
    face = _faces(_box())[0]
    report = check_faithful(
        pool,
        _write_brep(edge, tmp_path / "o.brep"),
        _write_brep(face, tmp_path / "r.brep"),
        declared_added=[],
        declared_removed=[],
    )
    assert report.ok is False and "area_changed" in report.reasons
    assert report.area_ratio is None
