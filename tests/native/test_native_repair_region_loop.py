# Responsibility: Verify on the REAL kernel that the region loop proves each region with a scan
#                 scoped to it, pays exactly two whole-shape scans for the run, and ships what
#                 committed even when another region is queued for a person.
from __future__ import annotations

import pytest

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair import session as session_module
from meshpipeline.cad.repair.caps import measure
from meshpipeline.cad.repair.loop import LoopBudget
from meshpipeline.cad.repair.package import PackageBudget
from meshpipeline.cad.repair.region_loop import run_regions
from meshpipeline.cad.repair.rules_author import rules_author

pytest.importorskip("OCP.ShapeFix", reason="the CAD kernel is a native-tier dependency")

#: Two 100 mm plates 300 mm apart, each with a sliver notch cut into it. BUILT HERE RATHER THAN
#: READ: what makes this shape evidence is that its defects sit in two places far enough apart
#: that the clustering cannot merge them, which is a property of the construction and not of a
#: file. Each notch leaves a 0.02 mm2 sliver FACE and two 0.002 mm edges - the boolean-that-just-
#: missed signature `write_small_edge_step` documents - and `FixSmallFaces` is the one catalogue
#: operation that removes a sliver face, so each region has a repair available to it.
GAP = 300.0


@pytest.fixture(autouse=True)
def _opted_in(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_DEVIATION_RATIO", 0.001)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_TOLERANCE_MM", 0.1)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ALLOW_FACE_REMOVAL", False)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_AREA_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO", 0.02)
    # The sliver sweep is what the rules author will reach for here; it declares every face it
    # removes and is refused the moment the count disagrees, which is why it may be opted into.
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
    """The two plates, through a STEP round trip - which is how a customer's part arrives.

    THE ROUND TRIP IS NOT DECORATION. It is what gives the localiser the topology a real upload
    has; a compound assembled in memory keeps the boolean's own sub-shape sharing and the scan
    reports a different part.
    """
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
    return _read_shape(path)


class _ScanSpy:
    """Every `localize_scan` the run made, and whether it was scoped to a region."""

    def __init__(self, monkeypatch) -> None:
        self.within: list = []
        real = session_module._localize_scan

        def spy(shape, **kwargs):
            self.within.append(kwargs.get("within"))
            return real(shape, **kwargs)

        # The session looks its scanner up on the module at call time, which is what makes this
        # observable without touching the loop.
        monkeypatch.setattr(session_module, "_localize_scan", spy)

    @property
    def scoped(self) -> list:
        return [region for region in self.within if region is not None]

    @property
    def whole_shape(self) -> list:
        return [region for region in self.within if region is None]


def _author(shape, **kwargs):
    return rules_author(shape, original=measure(shape), **kwargs)


def _budget() -> LoopBudget:
    return LoopBudget(max_ops=6, stall_limit=2, timeout_s=120.0)


def test_a_packaged_region_is_proved_by_a_scan_scoped_to_it(tmp_path, monkeypatch):
    """THE FIRST TIME THE SCOPED PATH MEETS A REAL KERNEL inside a whole run.

    Every earlier task exercised `localize_scan(..., within=)` with doubles or on its own; here it
    is what a commit rests on.
    """
    shape = _two_notched_plates(tmp_path)
    before, complete = session_module._localize_scan(shape, face_scan_s=0.0)
    assert complete and len(before) == 6, [d.to_dict()["entity"] for d in before]

    spy = _ScanSpy(monkeypatch)
    run = run_regions(shape, author=_author(shape), tolerance_mm=0.1, budget=_budget())

    assert len(run.regions) == 2 and all(region.located for region in run.regions)
    # ONE PACKAGE FOR TWO REGIONS, and not a bug: `fix_small_faces` is a whole-shape sweep that
    # re-measures every face it was aimed at, so region 0's commit legitimately took region 1's
    # sliver with it. The loop then re-read region 1's defects from the CURRENT scan, found
    # nothing, and left it alone rather than spending a package to be told the defect is gone.
    assert [(p.region_index, p.status) for p in run.packages] == [(0, "committed")]

    # THE PROOF OF A REGION IS SCOPED TO IT, and the region it names is the one being packaged.
    assert [region.index for region in spy.scoped] == [p.region_index for p in run.packages]
    # AND THE RUN'S OWN VERDICT IS NOT. Two whole-shape inspections belong to this loop - the
    # first scan and the one after the merge; the rest are `try_op`'s per-operation
    # re-inspections, which the operation-at-a-time loop pays too.
    assert run.timings["verification_scans"] == 2
    assert run.timings["scoped_scans"] == len(run.packages)
    assert len(spy.whole_shape) == 2 + sum(len(p.verdicts) for p in run.packages)
    assert run.timings["scan_count"] == 4   # first, the operation's own, the scoped proof, final

    assert run.stopped_because == "resolved"
    assert run.complete_scan is True
    assert run.defects_after == ()
    # The upload's own scan travels with the run, so a later stage can still say what arrived.
    assert len(run.defects_before) == 6


def test_a_committed_region_ships_while_another_is_queued(tmp_path, monkeypatch):
    """Spec decision 7, on the real kernel: a hard region queues and the clean ones finish.

    The FIRST region is given a package it cannot afford (a zero-operation allowance), which is
    how a real region runs out of its budget without satisfying its claim. It is the first one
    deliberately: the walk must carry on past it rather than end the run there, which is exactly
    what the operation-at-a-time loop's global stall limit cannot do.
    """
    shape = _two_notched_plates(tmp_path)
    original = measure(shape)
    broke = PackageBudget(max_ops=0, timeout_s=120.0, model_calls=0, deviation_mm=0.0)
    generous = _author(shape)
    starved = _author(shape, budget=broke)

    def author(region, defects):
        return (starved if region.index == 0 else generous)(region, defects)

    spy = _ScanSpy(monkeypatch)
    run = run_regions(shape, author=author, tolerance_mm=0.1, budget=_budget())

    assert run.escalated == (0,)
    assert [(p.region_index, p.status) for p in run.packages] == [(0, "escalated"),
                                                                  (1, "committed")]
    assert "budget" in run.packages[0].reason
    # A QUEUED REGION IS NOT A ROLLED-BACK RUN: region 1's repair is in the shape handed back.
    after = measure(run.shape)
    assert after.faces < original.faces
    assert run.stopped_because == "escalated"
    assert run.resolved is False
    # The escalated region cost no scan at all - its package never ran, so the only scoped proof
    # is the one the region that committed rests on.
    assert spy.scoped == [run.regions[1]]


def test_the_posture_selects_the_region_loop_in_the_entry_point(tmp_path, monkeypatch):
    """The switch, end to end: `repair_step_file` drives regions and the report says so.

    The after-checks are deliberately left alone here - the point is that a region run goes
    through exactly the ones an operation-at-a-time run does, so a package can never buy an
    exemption from the caps that protect the customer's geometry.
    """
    from meshpipeline.cad.repair.conservative import _write_step, repair_step_file

    monkeypatch.setattr(rcfg, "CAD_REPAIR_REGION_LOOP", "live")
    source, destination = tmp_path / "in.step", tmp_path / "out.step"
    _write_step(_two_notched_plates(tmp_path), source)

    report = repair_step_file(source, destination)
    measurements = {m.name: m.value for m in report.measurements}

    assert measurements["planner"] == "regions-rules-v1"
    assert measurements["stopped_because"] == "resolved"
    assert measurements["regions"]["escalated"] == []
    assert [p["status"] for p in measurements["regions"]["packages"]] == ["committed"]
    assert measurements["timings"]["verification_scans"] == 2
    assert measurements["operations_kept"] == 1
    assert destination.exists()


def test_a_directive_this_driver_cannot_play_is_reported_rather_than_dropped(tmp_path,
                                                                              monkeypatch):
    """An engineer's answer is `try_op`'s `answered_held` exemption, and a package cannot carry
    one - so this driver cannot play a directive. What it must not do is lose it silently."""
    from meshpipeline.cad.repair.conservative import _write_step, repair_step_file

    monkeypatch.setattr(rcfg, "CAD_REPAIR_REGION_LOOP", "live")
    source, destination = tmp_path / "in.step", tmp_path / "out.step"
    _write_step(_two_notched_plates(tmp_path), source)

    report = repair_step_file(source, destination,
                              directives=({"item_id": "i-1", "op": "fix_small_faces"},))
    measurements = {m.name: m.value for m in report.measurements}

    assert [r["refusal"] for r in measurements["refusals"]] == ["directive_unplayable"]
    assert measurements["refusals"][0]["reason"] == "region_loop_posture"
    assert measurements["refusals"][0]["directive"]["item_id"] == "i-1"
