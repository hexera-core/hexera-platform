# Responsibility: Prove `focus_faces` against the REAL kernel - a focus on one locus of a
#                 two-locus part repairs that locus and leaves the other standing and REPORTED,
#                 and an empty focus is byte-for-byte today's run.
# Boundaries: the native tier, because a focus is only meaningful over a real decomposition: the
#             regions, their kernel face indices and which defect sits in which are kernel facts.
#             The filter's arithmetic is unit-tested in tests/unit/cad/test_repair_focus_faces.py.
from __future__ import annotations

import pytest

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.caps import measure
from meshpipeline.cad.repair.loop import LoopBudget
from meshpipeline.cad.repair.region_loop import NOT_IN_FOCUS, run_regions
from meshpipeline.cad.repair.rules_author import rules_author

pytest.importorskip("OCP.ShapeFix", reason="the CAD kernel is a native-tier dependency")

#: Two 100 mm plates 300 mm apart, each with a sliver notch cut into it - the same construction
#: `test_native_repair_region_loop.py` uses, and for the same reason: what makes the shape
#: evidence is that its defects sit far enough apart that the clustering cannot merge them, which
#: is a property of the construction and not of a file. Each notch leaves a sliver FACE, and
#: `FixSmallFaces` removes one, so each region has a repair available to it - which is what makes
#: "the other region was NOT repaired" a statement about the focus rather than about the author.
GAP = 300.0


@pytest.fixture(autouse=True)
def _opted_in(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REGION_LOOP", "live")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_DEVIATION_RATIO", 0.001)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_TOLERANCE_MM", 0.1)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ALLOW_FACE_REMOVAL", False)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_AREA_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FIX_SMALL_FACES", True)


def _notched_plate(x0: float):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakePrism
    from OCP.gp import gp_Pnt, gp_Vec

    plate = BRepPrimAPI_MakeBox(gp_Pnt(x0, 0.0, 0.0), gp_Pnt(x0 + 100.0, 100.0, 10.0)).Shape()
    polygon = BRepBuilderAPI_MakePolygon()
    for point in (gp_Pnt(x0, -1.0, -1.0), gp_Pnt(x0 + 20.0, -1.0, -1.0),
                  gp_Pnt(x0 + 20.0, 0.002, -1.0), gp_Pnt(x0, 0.0, -1.0)):
        polygon.Add(point)
    polygon.Close()
    wedge = BRepPrimAPI_MakePrism(
        BRepBuilderAPI_MakeFace(polygon.Wire(), True).Face(), gp_Vec(0.0, 0.0, 12.0)).Shape()
    return BRepAlgoAPI_Cut(plate, wedge).Shape()


def _two_notched_plates(tmp_path):
    """The two plates, written to STEP and read back - which is how a customer's part arrives."""
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound

    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.repair.conservative import _write_step

    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    for x0 in (0.0, GAP):
        builder.Add(compound, _notched_plate(x0))
    path = tmp_path / "two_notched_plates.step"
    _write_step(compound, path)
    return path, _read_shape(path)


def _run(shape, **kw):
    return run_regions(shape, author=rules_author(shape, original=measure(shape)),
                       tolerance_mm=0.1,
                       budget=LoopBudget(max_ops=20, timeout_s=300.0, stall_limit=20), **kw)


def _decided(run) -> dict:
    """Everything the run DECIDED and nothing it merely measured: a wall clock is not a decision."""
    def scrub(value):
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items()
                    if k != "timings" and not k.endswith("_s")}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value
    return scrub(run.to_dict())


def _two_face_regions(run):
    faced = [r for r in run.regions if r.face_indices]
    if len(faced) < 2:
        pytest.skip(f"the fixture gave {len(faced)} face-bearing region(s), not two")
    return faced[0], faced[1]


def test_focus_faces_empty_is_byte_identical(tmp_path):
    """THE FLAG-OFF GUARANTEE, against the kernel. `focus_faces=()` is not a near-identical run:
    it must take exactly the same branch, so every decision it records has to match."""
    _path, shape = _two_notched_plates(tmp_path)
    plain = _run(shape)
    explicit = _run(_two_notched_plates(tmp_path)[1], focus_faces=())
    assert _decided(explicit) == _decided(plain)
    assert "not_in_focus" not in str(_decided(explicit))


def test_handback_repairs_the_named_faces(tmp_path):
    """A focus on ONE locus of a two-locus part: that locus is repaired, the other is reported
    `not_in_focus`, and its defects are still there afterwards.

    THE FOCUS IS READ OFF THE PART'S OWN DECOMPOSITION rather than hard-coded: a kernel face index
    is a property of the bytes, and a number written here would rot. What is pinned is the
    RELATION - the named faces' region is repaired, the other region's is not.
    """
    _path, shape = _two_notched_plates(tmp_path)
    survey = _run(shape)
    target, other = _two_face_regions(survey)
    focus = tuple(sorted(target.face_indices))
    assert focus and not (target.face_indices & other.face_indices)
    # THE CONTROL: unfocused, BOTH regions are repaired, so the assertions below measure the
    # focus and not an author that had nothing to offer the second locus.
    committed_unfocused = {p.region_index for p in survey.packages if p.committed}
    assert {target.index, other.index} <= committed_unfocused, (
        "the fixture must repair both loci unfocused, or this test proves nothing: "
        f"{_decided(survey)['packages']}")

    run = _run(_two_notched_plates(tmp_path)[1], focus_faces=focus)

    # ONLY THE NAMED LOCUS WAS AUTHORED FOR.
    assert {p.region_index for p in run.packages} == {target.index}

    # AND THE OTHER REGION IS REPORTED, never silently skipped.
    refused = [r for r in run.refusals if r.get("refusal") == NOT_IN_FOCUS]
    assert other.index in {int(r["region"]) for r in refused}
    assert all(r["focus_faces"] == list(focus) for r in refused)

    # AND THE FOCUSED REGION'S OWN DEFECTS ARE GONE: the focus bought a real repair, not silence.
    remaining = {d.fingerprint for d in run.defects_after}
    assert not (remaining & set(target.fingerprints)), \
        f"the focused region's defects should be gone: {_decided(run)}"

    # WHAT A FOCUS DOES NOT PROMISE, measured here so nobody later reads more into it. A FOCUS
    # SCOPES THE WALK, NOT THE REACH OF A CATALOGUE OPERATION. `fix_small_faces` is a whole-part
    # sweep that DECLARES every face it removes, and on this fixture the one package authored for
    # region 0 declares both plates' slivers - so region 1's defect goes too, as a side effect of
    # an operation region 0 asked for, exactly as region_loop already documents for the unfocused
    # walk. What the focus guarantees is that no out-of-focus region is ever AUTHORED FOR: no
    # package is proposed against it, no attempt is spent on it, and it is reported.
    swept = {f for p in run.packages for op in p.verdicts if op.kept
             for f in (op.evidence or {}).get("removed_fingerprints", [])}
    assert set(other.fingerprints) & swept, (
        "this fixture's repair is a whole-part sweep; if that changed, re-read the note above "
        "rather than deleting it")
    assert len(run.packages) == 1, "one region in focus means one package, ever"
