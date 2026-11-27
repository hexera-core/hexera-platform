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
    # THESE TESTS ARE ABOUT THE v3 CONSERVATIVE REPAIR (its loop, caps and report keys). Since the CRS v5 beta
    # (#540) `CAD_REPAIR_ENGINE` defaults to v5, so the engine they exercise is named here, not inherited.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENGINE", "v3")
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

    def repeating(region, defects, attempts):
        package = author(region, defects, attempts)
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
    # it a package, and that package's own operation DECLINES ("not_small") on geometry already
    # gone.
    # SUCH A PACKAGE IS REVERTED, NOT COMMITTED, and that is the safety rule rather than a cost
    # one: it kept nothing, so its claim to have CLEARED a defect is a claim about geometry it
    # never touched, and `package.evaluate` refuses it however the scoped scan names things. The
    # region is then offered another package, and this one really repairs - the sliver edges the
    # notch left - so the region commits on work it did.
    assert [(p.region_index, p.status) for p in run.packages] == [(0, "committed"),
                                                                   (1, "reverted"),
                                                                   (1, "committed")]
    assert [v.status for p in run.packages for v in p.verdicts] == ["kept", "rolled_back", "kept"]
    assert [v.op for p in run.packages for v in p.verdicts] == ["fix_small_faces",
                                                                 "fix_small_faces",
                                                                 "fix_small_edges"]

    # THE PROOF OF A REGION IS SCOPED TO IT, and so is the proof of every operation inside it,
    # which is the whole cost thesis: region 0 paid one scoped scan for its operation and one for
    # its package; region 1's first operation was refused before any re-inspection, so that
    # attempt paid only the package's, and its second attempt paid one of each.
    assert [region.index for region in spy.scoped] == [0, 0, 1, 1, 1]
    # AND THE RUN'S OWN VERDICT IS NOT. Exactly two whole-shape inspections belong to this run,
    # whatever its plans contained - the first scan and the one after the merge.
    assert run.timings["verification_scans"] == 2
    assert run.timings["scoped_scans"] == len(spy.scoped)
    assert len(spy.whole_shape) == 2
    assert run.timings["scan_count"] == 7   # the two above, plus the five scoped ones

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

    def author(region, defects, attempts):
        return (starved if region.index == 0 else generous)(region, defects, attempts)

    spy = _ScanSpy(monkeypatch)
    # ONE ATTEMPT PER REGION, pinned: region 0's package cannot afford its own plan, so it
    # escalates unapplied and would do so again on every attempt the bound allows. What this
    # probe is about is the walk carrying past the queued region, not how often it is offered one.
    run = run_regions(shape, author=author, tolerance_mm=0.1, budget=_budget(), attempts=1)

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
    # Region 1's first package aimed at a sliver region 0's whole-shape sweep had already taken,
    # so its operation declined, it kept nothing and its `cleared` claim was refused; its second
    # package repaired the notch's sliver edges instead. See the probe above.
    assert [p["status"] for p in measurements["regions"]["packages"]] == ["committed",
                                                                          "reverted",
                                                                          "committed"]
    assert measurements["timings"]["verification_scans"] == 2
    assert measurements["operations_kept"] == 2
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


def test_the_rules_author_revises_after_an_attempt_was_reverted(tmp_path):
    """THE LEARNING, on the real kernel and through the real planner.

    This is what the operation-at-a-time loop got from a `RunHistory` that outlived one iteration
    and the region driver could not express: told that an operation was tried on a defect and did
    not survive, the rules propose something else. Written against real geometry rather than a
    stub planner because the thing under test is the catalogue's ordering, not a double's.
    """
    from meshpipeline.cad.repair.regions import decompose
    from meshpipeline.cad.repair.session import OpVerdict, PackageVerdict

    shape = _two_notched_plates(tmp_path)
    before, complete = session_module._localize_scan(shape, face_scan_s=0.0)
    assert complete
    region = decompose(before, diagonal_mm=measure(shape).diagonal_mm)[0]
    mine = tuple(d for d in before if d.fingerprint in region.fingerprints)
    author = _author(shape)

    first = author(region, mine, ())
    assert first is not None
    attempted = first.plan[0]

    # THE VERDICT THE LOOP WOULD HAND BACK: the operation ran, did not survive, and the package
    # was discarded with it.
    reverted = PackageVerdict(
        region_index=region.index, status="reverted",
        reason="cleared defects still present: " + sorted(first.predicate.cleared)[0],
        verdicts=(OpVerdict(op=attempted["op"].name,
                            targets=tuple(r.fingerprint for r in attempted["targets"]),
                            defect_code=attempted["defect_code"].value, status="rolled_back",
                            reason="target_remains"),))

    second = author(region, mine, (reverted,))

    # NOT THE SAME PAIR. Either a different operation for the same defect or the next defect in
    # the region - what the rules would have proposed on the baseline's next iteration - and
    # never the attempt that was just rolled back. On this fixture it is the sliver EDGES after
    # the sliver FACE, which is only evidence while the plate keeps both.
    assert second is not None
    assert (second.plan[0]["op"].name, sorted(second.predicate.cleared)) != (
        attempted["op"].name, sorted(first.predicate.cleared))
    # AND THE FIRST CALL OF A REGION IS UNCHANGED BY ALL OF THIS: an empty `attempts` is an empty
    # ledger, so a region's first package is what the one-package-per-region walk authored.
    again = author(region, mine, ())
    assert again is not None
    assert (again.plan[0]["op"].name, sorted(again.predicate.cleared)) == (
        attempted["op"].name, sorted(first.predicate.cleared))


# ---------------------------------------------------------------------------
# THE PAIRED-RIM GUARD, WHICH IS NOT A REGION'S PROPERTY
#
# A through-hole whose wall is missing leaves two rims, one at each end of the passage, and
# patching both seals it - a plate handed back with no bolt hole. The guard that refuses this
# recomputes the pairing from the defect list it is HANDED, and this driver hands it one region's
# defects at a time. The two rims of a 10 mm plate's bore decompose into two regions (asserted
# below, because the fixture is only evidence while that holds), so without the whole-part view
# the pair is invisible: measured on this fixture, the pre-fix driver filled BOTH rims, committed
# both packages and called the run `resolved` with no refusal recorded at all.
# ---------------------------------------------------------------------------


@pytest.fixture
def _filling_on(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FILL_PLANAR_HOLES", True)
    # Wide enough that the lone rim below is really filled rather than declined as too large -
    # the test that the guard did not cost the case filling exists for has to see a fill.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_HOLE_SPAN_RATIO", 0.9)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_AREA_CHANGE_RATIO", 0.05)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO", 0.05)


def _region_run(shape):
    return run_regions(shape, author=_author(shape), tolerance_mm=0.1,
                       budget=LoopBudget(max_ops=20, timeout_s=300.0, stall_limit=20))


def _filled(run) -> set:
    """Every fingerprint a KEPT, COMMITTED fill actually patched."""
    return {target for verdict in run.packages if verdict.committed
            for op in verdict.verdicts if op.kept and op.op == "fill_planar_hole"
            for target in op.targets}


def test_two_rims_of_one_passage_in_different_regions_are_both_refused(tmp_path, _filling_on):
    from tests.cad_fixtures import write_missing_bore_wall_step

    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.repair.localize import localize_scan
    from meshpipeline.cad.repair.plan import paired_rim_fingerprints

    shape = _read_shape(write_missing_bore_wall_step(tmp_path / "bore.step"))
    defects, _ = localize_scan(shape)
    rims = paired_rim_fingerprints(shape, defects)
    assert len(rims) == 2, f"the fixture must present a PAIR of rims, got {sorted(rims)}"

    run = _region_run(shape)

    # THE FIXTURE'S OWN PRECONDITION. If the two rims ever land in one region the part pairs them
    # the old way and this test proves nothing, so it must fail loudly rather than pass quietly.
    holders = {rim: [r.index for r in run.regions if rim in r.fingerprints] for rim in rims}
    assert all(len(where) == 1 for where in holders.values()), holders
    assert len({where[0] for where in holders.values()}) == 2, (
        f"both rims decomposed into one region, so the guard never needed the part: {holders}")

    assert _filled(run) == set(), (
        "a rim of a missing tube wall was patched - the customer's through-hole is sealed")
    refused = {record["defect"] for record in run.refusals
               if record["reason"] == "paired_rim"}
    assert refused == set(rims), (
        f"the refusal a customer is owed did not reach the run: {run.refusals}")


def test_the_paired_rim_refusal_is_deterministic(tmp_path, _filling_on):
    # THE WHOLE BEFORE/AFTER COMPARISON RESTS ON THIS, so the part-level set the guard now carries
    # must not make a run depend on anything but its bytes.
    from tests.cad_fixtures import write_missing_bore_wall_step

    from meshpipeline.cad.repair.brep import _read_shape

    path = write_missing_bore_wall_step(tmp_path / "bore.step")
    first, second = (_region_run(_read_shape(path)) for _ in range(2))

    def decided(run):
        # EVERYTHING THE RUN DECIDED, and nothing it merely measured: a wall clock is not a
        # decision, so the elapsed keys come out at both levels and the rest must match exactly.
        def scrub(value):
            if isinstance(value, dict):
                return {k: scrub(v) for k, v in value.items()
                        if k != "timings" and not k.endswith("_s")}
            if isinstance(value, list):
                return [scrub(item) for item in value]
            return value

        return scrub(run.to_dict())

    assert decided(first) == decided(second)


def test_a_lone_rim_in_its_own_region_is_still_filled(tmp_path, _filling_on):
    # THE GUARD MUST NOT COST THE CASE FILLING EXISTS FOR. An ordinary single opening has nothing
    # to pair with anywhere on the part, so widening what the pairing SEES must not widen what it
    # refuses: this hole is still patched.
    from tests.cad_fixtures import write_open_shell_step

    from meshpipeline.cad.repair.brep import _read_shape

    shape = _read_shape(write_open_shell_step(tmp_path / "holed.step"))
    run = _region_run(shape)

    assert _filled(run), f"the legitimate hole was not filled: {run.to_dict()['packages']}"
    assert [r for r in run.refusals if r["reason"] == "paired_rim"] == []


def test_a_file_no_operation_answers_ends_unmeshable_not_resolved(monkeypatch):
    """The probe names a failure no current operation answers, and the run says so.

    No committable fixture fails gmsh (synthetic overlaps mesh fine), so this reads the external
    mesh-failure corpus and skips when it is absent. This file fails with overlapping facets,
    located on a face, after OCP reads it.
    """
    import os
    from pathlib import Path

    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.repair.contracts import DefectCode
    from meshpipeline.cad.repair.loop import RESOLVED, UNMESHABLE

    corpus = Path(os.environ.get("HEXERA_MESH_FAIL_CORPUS",
                                 "~/archductor/mesh-fail-corpus")).expanduser()
    path = corpus / "abc/00500400_bce216c87d2d17025db6ab36_step_000.step"
    if not path.is_file():
        pytest.skip(f"mesh-failure corpus file {path} is absent (set HEXERA_MESH_FAIL_CORPUS)")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")

    shape = _read_shape(path)
    run = run_regions(shape, author=rules_author(shape, original=measure(shape)),
                      tolerance_mm=0.1, budget=_budget())

    assert run.stopped_because != RESOLVED
    codes = {d.code for d in run.defects_after}
    assert DefectCode.mesh_overlapping_facets in codes, codes
    assert not codes & {DefectCode.mesh_timeout, DefectCode.mesh_probe_error}, codes
    assert run.stopped_because == UNMESHABLE, run.stopped_because


def test_a_committed_fill_records_a_plausible_deviation(tmp_path, _filling_on, monkeypatch):
    """Under the mesh-probe posture a committed fill carries `deviation_mm`: measured on the real
    kernel, positive (a patch is new surface off the original) and far under the part."""
    from tests.cad_fixtures import write_open_shell_step

    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.repair.localize import _diagonal

    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE_TIMEOUT_S", 60.0)
    shape = _read_shape(write_open_shell_step(tmp_path / "holed.step"))
    run = _region_run(shape)

    measured = [p.deviation_mm for p in run.packages if p.committed]
    assert measured and all(d is not None for d in measured), run.to_dict()["packages"]
    assert max(measured) > 0.0
    assert max(measured) < _diagonal(shape)


def test_deviation_against_a_known_offset_pins_direction_and_scale():
    """A box face moved 1.0 mm off the original: the sampled value is exactly 1.0 (the geometry
    has no spread), which fixes both the direction (after-to-before) and the scale."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    from meshpipeline.cad.repair.deviation import measure_deviation

    before = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), gp_Pnt(10, 10, 10)).Shape()
    after = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), gp_Pnt(11, 10, 10)).Shape()  # +x face at 11
    result = measure_deviation(before, after)
    assert result.mm is not None and abs(result.mm - 1.0) < 1e-6, result
    assert result.faces and result.samples >= 4 * result.faces


def test_a_mesher_problem_verdict_reaches_the_record_and_the_receipt(tmp_path, monkeypatch):
    """END TO END on the real kernel: a model author (live, its call faked) answers every region
    with a mesher-problem verdict. The verdict must land in the report's `model_author` record and
    the receipt built from the record the attempt node forwards must render it."""
    import re

    from meshpipeline.cad.repair import model_author as ma
    from meshpipeline.cad.repair.conservative import (
        RepairRefused,
        _write_step,
        repair_step_file,
    )
    from meshpipeline.pipeline import repair_receipt as receipt
    from meshpipeline.pipeline.repair_attempt import _forwarded

    async def ask(system, user, *, job_id, timeout_s):
        fingerprint = re.search(r'"fingerprint":\s*"([^"]+)"', user).group(1)
        return {"verdict": {"mesher_problem": [fingerprint], "reason": "the notch is sound"}}

    monkeypatch.setattr(ma, "_ask", ask)
    monkeypatch.setattr(ma, "_configured", lambda: True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REGION_LOOP", "live")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_LLM_AUTHOR", "live")
    source, destination = tmp_path / "in.step", tmp_path / "out.step"
    _write_step(_two_notched_plates(tmp_path), source)

    # A run whose only outcome is a verdict keeps nothing, so the real entry point REFUSES it
    # ("nothing to promote"); the loop's measurements travel in the refusal, exactly as the
    # attempt node's `_declined` carries them to the receipt.
    with pytest.raises(RepairRefused) as refused:
        repair_step_file(source, destination)
    measured = dict(refused.value.measurements)

    verdicts = measured["model_author"]["verdicts"]
    assert verdicts and all(v["author"] == ma.LABEL and v["mesher_problem"] and v["reason"]
                            for v in verdicts)

    record = {"attempted": False, "outcome": "refused", "route": "conservative_repair",
              "detail": {"measurements": _forwarded(measured)}}
    state = {"job_id": "j", "repair_report": {"status": "repairable", "report": {
        "summary": "s", "defects": [], "entities": []}}}
    rows = receipt.repair_receipt_rows(state, {"repair_attempt": record})
    row = next(r for r in rows if "judged this geometry sound" in r["label"])
    assert verdicts[0]["mesher_problem"][0] in row["why"]
