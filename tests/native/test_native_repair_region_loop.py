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


def _repeating_author(shape, plan_ops: int):
    """The rules author, with its plan item repeated `plan_ops` times.

    A LONGER PLAN WITHOUT A SECOND KIND OF REPAIR. The catalogue offers one operation that
    removes a sliver face, so the honest way to lengthen a plan here is to ask for the same one
    twice; the second application finds nothing small left and declines, which is a real plan
    item with a real verdict and is exactly what a model author's over-long plan would look like.
    """
    # The author's own allowance is ONE operation ("a budget is a bound on what the author asked
    # for"), so a longer plan needs an allowance that admits it - otherwise the package escalates
    # unapplied and the run measures nothing.
    author = _author(shape, budget=PackageBudget(max_ops=plan_ops, timeout_s=120.0,
                                                 model_calls=0, deviation_mm=0.0))

    def repeating(region, defects):
        package = author(region, defects)
        if package is None or not package.plan:
            return package
        from dataclasses import replace
        return replace(package, plan=tuple(dict(package.plan[0]) for _ in range(plan_ops)))

    return repeating


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
    # BOTH REGIONS ARE PACKAGED, and that is the price of locality rather than a bug.
    # `fix_small_faces` is a whole-shape sweep, so region 0's commit legitimately took region 1's
    # sliver with it - but nothing has walked region 1 since, so the loop does not know, offers
    # it its one package, and that package's own operation DECLINES ("not_small") on geometry
    # already gone. The region commits on the scoped proof that it is clean; no operation was
    # kept for doing nothing.
    assert [(p.region_index, p.status) for p in run.packages] == [(0, "committed"),
                                                                   (1, "committed")]
    assert [v.status for p in run.packages for v in p.verdicts] == ["kept", "rolled_back"]

    # THE PROOF OF A REGION IS SCOPED TO IT, and so is the proof of every operation inside it,
    # which is the whole cost thesis: region 0 paid one scoped scan for its operation and one for
    # its package; region 1's operation was refused before any re-inspection, so it paid only the
    # package's.
    assert [region.index for region in spy.scoped] == [0, 0, 1]
    # AND THE RUN'S OWN VERDICT IS NOT. Exactly two whole-shape inspections belong to this run,
    # whatever its plans contained - the first scan and the one after the merge.
    assert run.timings["verification_scans"] == 2
    assert run.timings["scoped_scans"] == len(spy.scoped)
    assert len(spy.whole_shape) == 2
    assert run.timings["scan_count"] == 5   # the two above, plus the three scoped ones

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
    # The escalated region cost no scan at all - its package never ran - so every scoped scan
    # here belongs to the region that committed: its operation's own proof, then the package's.
    assert spy.scoped == [run.regions[1], run.regions[1]]
    # WITH TWO REGIONS PACKAGED, this is what tells "paid once per RUN" from "once per REGION":
    # two whole-shape inspections belong to the loop however many regions and operations it took.
    assert len(spy.whole_shape) == 2


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
    assert [p["status"] for p in measurements["regions"]["packages"]] == ["committed",
                                                                          "committed"]
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


def test_a_committed_package_pays_two_whole_shape_scans_whatever_its_plan_length(tmp_path,
                                                                                  monkeypatch):
    """THE COST CLAIM OF THE WHOLE DESIGN, measured on the real kernel.

    "Local edit, local proof" is the justification for region decomposition, and until an
    operation INSIDE a package proved itself locally the loop paid a whole-shape walk after every
    operation and then a scoped scan on top - dearer than the operation-at-a-time baseline it
    exists to beat, measured on scans. What must be true now is that the number of whole-shape
    inspections is a property of the RUN (its first scan and its last), not of how much work the
    packages contained.

    PLAN LENGTH IS VARIED HERE DELIBERATELY: with one assertion against one plan, an
    implementation that scoped the package's proof and nothing else would still pass.
    """
    counts = {}
    for plan_ops in (1, 2):
        home = tmp_path / f"plan{plan_ops}"
        home.mkdir()
        shape = _two_notched_plates(home)
        spy = _ScanSpy(monkeypatch)
        run = run_regions(shape, author=_repeating_author(shape, plan_ops),
                          tolerance_mm=0.1, budget=_budget())
        planned = sum(len(p.verdicts) for p in run.packages)
        assert planned >= plan_ops and run.stopped_because == "resolved"

        # THE MEASUREMENT. Two whole-shape scans for the run, whatever the plans held.
        assert len(spy.whole_shape) == 2
        # And every other scan the session took was scoped to one region.
        assert len(spy.scoped) == run.timings["scan_count"] - 2
        assert run.timings["scoped_scans"] == len(spy.scoped)
        # ONE SCOPED SCAN PER PACKAGE AT LEAST - its proof - and at most one more per planned
        # operation. Not an exact count, and the slack is `try_op`'s own saving: an operation a
        # cap, a hold or its own `declined` answer refuses is "REFUSED BEFORE THE RE-INSPECTION
        # IS PAID FOR", so it takes no scan at all.
        assert len(run.packages) <= len(spy.scoped) <= planned + len(run.packages)
        counts[plan_ops] = (len(spy.whole_shape), len(spy.scoped), planned)

    # The second run really did plan more operations, and bought no whole-shape walk with them.
    assert counts[2][2] > counts[1][2]
    assert counts[2][0] == counts[1][0] == 2
    # IT DID NOT EVEN COST A SCOPED SCAN: the repeated operation declines on geometry the first
    # one already repaired, and `try_op` answers that before paying for any re-inspection. The
    # general bound is one scoped scan per extra operation and never a whole-shape one; here the
    # extra operations cost nothing at all, and the EQUALITY is asserted rather than the bound so
    # that the sentence above cannot outlive the behaviour it describes.
    assert counts[2][1] == counts[1][1]
