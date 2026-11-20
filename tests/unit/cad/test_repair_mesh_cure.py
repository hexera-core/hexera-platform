# Responsibility: Verify the keep predicate judges whether the DEFECT is gone - a failing part that newly meshes after an operation keeps it, even when the kernel finding at the target survives - while an unfixed part, a clean part, a new defect and a no-answer probe all still roll back.
from __future__ import annotations

import pytest

import meshpipeline.cad.repair.mesh_probe as mesh_probe
import meshpipeline.cad.repair.operations as operations
import meshpipeline.cad.repair.session as session_mod
import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.identity import EntityRef
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.mesh_probe import STAGE_3D
from meshpipeline.cad.repair.operations import FragmentBodies, OpOutcome
from meshpipeline.cad.repair.package import PackageBudget, Predicate, TargetPackage
from meshpipeline.cad.repair.regions import Region
from meshpipeline.cad.repair.session import RepairSession

MEASURE = Measure(valid=True, solids=1, shells=1, faces=20, edges=60, vertices=40,
                  diagonal_mm=200.0, max_tolerance_mm=0.01,
                  surface_area_mm2=1000.0, volume_mm3=8000.0)
_BOX = {"centroid": [5.0, 5.0, 5.0], "bbox_min": [0.0, 0.0, 0.0], "bbox_max": [10.0, 10.0, 10.0]}

# THE MEASURED CASE (abc 0052_00525324, T6): one zero-length edge, a kernel WARNING, on a part
# gmsh refuses with an unlocated "Two facets intersect". fix_small_edges leaves a small_edge at the
# target's box, and the whole part meshes afterwards.
_SHORT = EntityDefect(code=DefectCode.small_edge, severity=DefectSeverity.warning,
                      entity_type="edge", entity_index=1, message="short",
                      fingerprint="edge:short", location=dict(_BOX))
_FACETS = EntityDefect(code=DefectCode.mesh_other, severity=DefectSeverity.error,
                       entity_type="shape", entity_index=0,
                       message="PLC Error:  Two facets intersect at point",
                       measurements={"mesh_kind": "other", "gmsh_surfaces": [13], "stage": "3d"})
_TIMEOUT = EntityDefect(code=DefectCode.mesh_timeout, severity=DefectSeverity.error,
                        entity_type="shape", entity_index=0, message="ran past its budget",
                        measurements={"mesh_kind": "timeout", "gmsh_surfaces": []})
_NEW_ERROR = EntityDefect(code=DefectCode.open_shell, severity=DefectSeverity.error,
                          entity_type="edge", entity_index=7, message="free",
                          fingerprint="edge:new", location=dict(_BOX))
_REGION = Region(index=0, fingerprints=frozenset({"edge:short"}),
                 bbox_min=(0.0, 0.0, 0.0), bbox_max=(10.0, 10.0, 10.0))


@pytest.fixture(autouse=True)
def live(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE_TIMEOUT_S", 120.0)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_HEAL_SELF_INTERSECTIONS", False)
    # The STEP round-trip is the identity here; its own tests below make it bite.
    monkeypatch.setattr(session_mod, "_as_delivered", lambda shape: shape)


def _seq(monkeypatch, target, attr, results):
    calls: list = []

    def fake(shape, **kw):
        calls.append(kw.get("within"))
        return results[min(len(calls), len(results)) - 1]
    monkeypatch.setattr(target, attr, fake)
    return calls


class _FixEdges:
    name = "fix_small_edges"
    answers = frozenset({DefectCode.small_edge})
    scope = 1

    def targets_for(self, defect):
        return (EntityRef(entity_type=defect.entity_type, fingerprint=defect.fingerprint,
                          entity_index=defect.entity_index),)

    def apply(self, shape, targets, *, tolerance_mm):
        return OpOutcome(shape="fixed")


def _try(session, within=None):
    target = next(d for d in session.defects() if d.code is DefectCode.small_edge)
    op = _FixEdges()
    return session.try_op(op, op.targets_for(target), target.code, tolerance_mm=0.01,
                          within=within)


@pytest.mark.parametrize("within", [None, _REGION])
def test_a_failing_part_that_newly_meshes_keeps_the_operation(monkeypatch, within):
    # The kernel finding survives at the target; the reason the part would not mesh does not.
    _seq(monkeypatch, session_mod, "_localize_scan", [((_SHORT,), True)])
    _seq(monkeypatch, mesh_probe, "mesh_defects", [(_FACETS,), ()])
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    verdict = _try(session, within)
    assert verdict.kept
    # Counted separately, and the unmet claim stays on the verdict - never rewritten as cleared.
    assert verdict.reason == session_mod.MESHES_AFTER_TARGET_REMAINS
    assert verdict.delta["mesh_cured"] == ["mesh_other"]
    assert verdict.delta["unmet"] == ["edge:short"]
    assert "cleared" not in verdict.delta or "edge:short" not in verdict.delta["cleared"]
    assert session.shape == "fixed"
    # The surviving kernel finding stays on the list - honest - and the stale mesh failure goes.
    assert session.defects() == (_SHORT,)


def test_a_scoped_cure_is_proved_by_a_whole_shape_probe(monkeypatch):
    _seq(monkeypatch, session_mod, "_localize_scan", [((_SHORT,), True)])
    probes = _seq(monkeypatch, mesh_probe, "mesh_defects", [(_FACETS,), ()])
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    assert _try(session, _REGION).kept
    # first scan (whole), the scoped re-inspection, then the cure's own whole-shape proof
    assert probes == [None, _REGION, None]


@pytest.mark.parametrize("within", [None, _REGION])
def test_a_genuinely_unfixed_part_still_rolls_back(monkeypatch, within):
    _seq(monkeypatch, session_mod, "_localize_scan", [((_SHORT,), True)])
    _seq(monkeypatch, mesh_probe, "mesh_defects", [(_FACETS,)])
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    verdict = _try(session, within)
    assert not verdict.kept and verdict.reason == "target_remains"
    assert session.shape == "shape"


@pytest.mark.parametrize("within", [None, _REGION])
def test_a_clean_part_is_never_altered(monkeypatch, within):
    # It meshed before, so meshing after is no news: the leftover warning rolls the op back, and
    # no extra probe is paid for.
    _seq(monkeypatch, session_mod, "_localize_scan", [((_SHORT,), True)])
    probes = _seq(monkeypatch, mesh_probe, "mesh_defects", [()])
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    verdict = _try(session, within)
    assert not verdict.kept and verdict.reason == "target_remains"
    assert session.shape == "shape"
    assert len(probes) == 2


def test_a_part_whose_probe_never_answered_is_not_cured(monkeypatch):
    # A timeout is not a failure: nobody showed the part failed, so meshing after proves nothing.
    _seq(monkeypatch, session_mod, "_localize_scan", [((_SHORT,), True)])
    _seq(monkeypatch, mesh_probe, "mesh_defects", [(_TIMEOUT,), ()])
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    verdict = _try(session, _REGION)
    assert not verdict.kept and verdict.reason == "target_remains"


def test_a_cure_that_ran_out_of_probe_time_is_not_a_cure(monkeypatch):
    _seq(monkeypatch, session_mod, "_localize_scan", [((_SHORT,), True)])
    _seq(monkeypatch, mesh_probe, "mesh_defects", [(_FACETS,), (), (_TIMEOUT,)])
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    verdict = _try(session, _REGION)
    assert not verdict.kept and verdict.reason == "target_remains"


def test_a_cure_that_broke_something_else_is_still_new_defect(monkeypatch):
    _seq(monkeypatch, session_mod, "_localize_scan", [((_SHORT,), True), ((_SHORT, _NEW_ERROR), True)])
    _seq(monkeypatch, mesh_probe, "mesh_defects", [(_FACETS,), ()])
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    verdict = _try(session)
    assert not verdict.kept and verdict.reason == "new_defect"
    assert session.shape == "shape"


def test_the_flag_off_world_is_unchanged(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "off")
    _seq(monkeypatch, session_mod, "_localize_scan", [((_SHORT,), True)])
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    verdict = _try(session)
    assert not verdict.kept and verdict.reason == "target_remains"


def _package(op, defect, cleared):
    return TargetPackage(
        region=_REGION, intent="clear the short edge",
        plan=({"op": op, "targets": op.targets_for(defect), "defect_code": defect.code},),
        predicate=Predicate(cleared=frozenset(cleared), reclassified=frozenset(),
                            unchanged=frozenset(), max_area_delta_mm2=10.0,
                            max_volume_delta_mm3=10.0),
        budget=PackageBudget(max_ops=1, timeout_s=60.0, model_calls=0, deviation_mm=0.0))


def test_a_package_whose_operation_cured_the_part_commits(monkeypatch):
    # The rules author claims the short edge cleared; it is still there, but the part the package
    # was written to repair now meshes, proved by a whole-shape probe at the package boundary.
    _seq(monkeypatch, session_mod, "_localize_scan", [((_SHORT,), True)])
    probes = _seq(monkeypatch, mesh_probe, "mesh_defects", [(_FACETS,), ()])
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    session.defects()
    verdict = session.try_package(_package(_FixEdges(), _SHORT, {"edge:short"}))
    assert verdict.verdicts[0].kept, verdict.verdicts
    assert verdict.status == session_mod.COMMITTED, verdict.reason
    assert session.shape == "fixed"
    assert probes[-1] is None
    assert verdict.verdicts[0].reason == session_mod.MESHES_AFTER_TARGET_REMAINS
    assert verdict.unmet_cleared == frozenset({"edge:short"})
    assert verdict.to_dict()["unmet_cleared"] == ["edge:short"]


def test_an_ordinary_commit_reports_no_unmet_claim(monkeypatch):
    # The target really goes: the old door, byte-identical report.
    _seq(monkeypatch, session_mod, "_localize_scan", [((_SHORT,), True), ((), True)])
    _seq(monkeypatch, mesh_probe, "mesh_defects", [(_FACETS,), ()])
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    session.defects()
    verdict = session.try_package(_package(_FixEdges(), _SHORT, {"edge:short"}))
    assert verdict.status == session_mod.COMMITTED, verdict.reason
    assert verdict.verdicts[0].reason == ""
    assert "mesh_cured" not in verdict.verdicts[0].delta
    assert "unmet_cleared" not in verdict.to_dict()


def test_a_package_cure_undone_by_a_later_probe_reverts(monkeypatch):
    # The op's own proof said it meshed; the package boundary's whole-shape probe disagrees.
    _seq(monkeypatch, session_mod, "_localize_scan", [((_SHORT,), True)])
    _seq(monkeypatch, mesh_probe, "mesh_defects", [(_FACETS,), (), (), (), (_FACETS,)])
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    session.defects()
    verdict = session.try_package(_package(_FixEdges(), _SHORT, {"edge:short"}))
    assert verdict.status == session_mod.REVERTED
    assert "still present" in verdict.reason
    assert session.shape == "shape"


# FRAGMENT_BODIES ON A TOUCHING TWO-BODY PART (lane A's gate case, abc 00504025): the real
# operation, with the kernel seams doubled so the bodies touch (common volume 0) and the fuse adds
# the interface.

_THROUGH = EntityDefect(code=DefectCode.mesh_edge_through_face, severity=DefectSeverity.error,
                        entity_type="face", entity_index=4, message="a segment and a facet intersect",
                        fingerprint="face:wall", location=dict(_BOX),
                        measurements={"mesh_kind": "edge_through_face", "gmsh_surfaces": [4],
                                      "stage": STAGE_3D})
_THROUGH_UNLOCATED = EntityDefect(
    code=DefectCode.mesh_edge_through_face, severity=DefectSeverity.error, entity_type="shape",
    entity_index=0, message="PLC Error: A segment and a facet intersect at point",
    measurements={"mesh_kind": "edge_through_face", "gmsh_surfaces": [], "stage": STAGE_3D})


@pytest.fixture
def touching(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FRAGMENT_BODIES", True)
    monkeypatch.setattr(operations, "sub_shape", lambda shape, kind, index: "FACE")
    monkeypatch.setattr(operations, "_solid_count", lambda shape: 2)
    monkeypatch.setattr(operations, "_solid_owners", lambda shape, face: ("A",))
    monkeypatch.setattr(operations, "_meeting_partner", lambda shape, body, face: "B")
    monkeypatch.setattr(operations, "_face_owner_sets", lambda shape: [("A",), ("B",)])
    monkeypatch.setattr(operations, "_common_volume", lambda a, b: 0.0)
    monkeypatch.setattr(operations, "_general_fuse", lambda a, b, faces: ((("A2",), ("B2",)), "H"))
    monkeypatch.setattr(operations, "splice", lambda shape, pairs: ("SPLICED", "CTX"))
    tallies = {"shape": (2, 307, 750.0), "SPLICED": (2, 319, 750.0)}
    monkeypatch.setattr(operations, "_tally", lambda shape: tallies[shape])
    monkeypatch.setattr(operations, "_split_fingerprints", lambda before, after, **_: ("face:new",))
    monkeypatch.setattr(operations, "_diagonal_of", lambda shape: 200.0)
    monkeypatch.setattr(operations, "_face_names",
                        lambda body, **_: tuple(f"face:{body}:{n}" for n in (1, 2)))
    monkeypatch.setattr(operations, "mapping_from_context",
                        lambda *a, **k: operations.EntityMapping.empty())
    monkeypatch.setattr(session_mod, "_face_names_of", lambda shape: frozenset())
    _seq(monkeypatch, session_mod, "_localize_scan", [((), True)])
    return FragmentBodies()


def _fragment(session, op):
    target = session.defects()[0]
    return session.try_op(op, op.targets_for(target), target.code, tolerance_mm=0.01)


def test_fragment_on_touching_bodies_that_then_meshes_is_kept(monkeypatch, touching):
    _seq(monkeypatch, mesh_probe, "mesh_defects", [(_THROUGH,), ()])
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    verdict = _fragment(session, touching)
    assert verdict.kept, verdict.reason
    assert verdict.evidence["common_volume_mm3"] == 0.0
    assert session.shape == "SPLICED"


def test_fragment_on_touching_bodies_that_still_fails_rolls_back(monkeypatch, touching):
    # The measured gate part: the interface is added, the face can no longer be named, and gmsh
    # still fails. Not a repair, and the predicate must not call it one.
    _seq(monkeypatch, mesh_probe, "mesh_defects", [(_THROUGH,), (_THROUGH_UNLOCATED,)])
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    verdict = _fragment(session, touching)
    assert not verdict.kept and verdict.reason == "target_remains"
    assert verdict.evidence["faces_added"] == 12
    assert session.shape == "shape"


# THE CURE IS PROVED ON THE PART AS DELIVERED (abc 0052_00525324, measured): fix_small_edges cures
# the part by widening tolerances; the in-memory shape and its BREP round-trip mesh, and its STEP
# round-trip - the file a customer receives - does not.


def _probe_by_shape(monkeypatch, verdicts):
    seen: list = []

    def fake(shape, **kw):
        seen.append(shape)
        return verdicts[shape]
    monkeypatch.setattr(mesh_probe, "mesh_defects", fake)
    return seen


@pytest.mark.parametrize("within", [None, _REGION])
def test_a_cure_lost_in_the_step_export_is_not_a_cure(monkeypatch, within):
    _seq(monkeypatch, session_mod, "_localize_scan", [((_SHORT,), True)])
    monkeypatch.setattr(session_mod, "_as_delivered", lambda shape: f"{shape}.step")
    _probe_by_shape(monkeypatch, {"shape": (_FACETS,), "fixed": (), "fixed.step": (_FACETS,)})
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    verdict = _try(session, within)
    assert not verdict.kept and verdict.reason == "target_remains"
    assert session.shape == "shape"


def test_a_cure_that_survives_the_step_export_is_kept(monkeypatch):
    _seq(monkeypatch, session_mod, "_localize_scan", [((_SHORT,), True)])
    monkeypatch.setattr(session_mod, "_as_delivered", lambda shape: f"{shape}.step")
    seen = _probe_by_shape(monkeypatch, {"shape": (_FACETS,), "fixed": (), "fixed.step": ()})
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    assert _try(session, _REGION).kept
    assert seen[-1] == "fixed.step"


def test_an_export_that_fails_proves_no_cure(monkeypatch):
    _seq(monkeypatch, session_mod, "_localize_scan", [((_SHORT,), True)])
    monkeypatch.setattr(session_mod, "_as_delivered", lambda shape: None)
    _seq(monkeypatch, mesh_probe, "mesh_defects", [(_FACETS,), ()])
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    assert not _try(session, _REGION).kept


def test_as_delivered_round_trips_a_real_solid_through_step():
    pytest.importorskip("OCP")
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.GProp import GProp_GProps

    box = BRepPrimAPI_MakeBox(10.0, 20.0, 30.0).Shape()
    back = session_mod._as_delivered(box)
    assert back is not None and not back.IsNull()
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(back, props)
    assert props.Mass() == pytest.approx(6000.0, rel=1e-6)


def test_an_operation_that_changed_nothing_is_never_cured(monkeypatch):
    # A STEP round-trip alone may change gmsh's answer; that is the export's doing, never the
    # operation's, so an op handing back the very shape it was given cannot take the cure door.
    class _Same(_FixEdges):
        def apply(self, shape, targets, *, tolerance_mm):
            return OpOutcome(shape=shape)

    _seq(monkeypatch, session_mod, "_localize_scan", [((_SHORT,), True)])
    _seq(monkeypatch, mesh_probe, "mesh_defects", [(_FACETS,), ()])
    session = RepairSession("shape", measurer=lambda s: MEASURE)
    target = session.defects()[0]
    verdict = session.try_op(_Same(), _Same().targets_for(target), target.code,
                             tolerance_mm=0.01, within=_REGION)
    assert not verdict.kept and verdict.reason == "target_remains"


# THE INVESTIGATOR'S CLONE SEES THE WHOLE PART'S MESH FAILURE (Greptile PRRT_kwDOUFq_Ps656SQJ).
# A scratch clone is seeded with ONE REGION's defects - here only the short edge - so the part's
# unlocated "Two facets intersect" is not on its list. Without the standing codes the cure cannot
# see that the part failed, and the edit that makes it mesh is rolled back.


def _clone(monkeypatch, standing=frozenset()):
    from meshpipeline.cad.repair import investigator_tools

    monkeypatch.setattr(investigator_tools, "_clone", lambda shape: shape)
    _seq(monkeypatch, session_mod, "_localize_scan", [((_SHORT,), True)])
    probes = _seq(monkeypatch, mesh_probe, "mesh_defects", [()])
    scratch = investigator_tools.Scratch.of("shape", original=MEASURE, defects=(_SHORT,),
                                            measurer=lambda s: MEASURE, standing_mesh=standing)
    return scratch, probes


def test_a_region_clone_with_the_parts_standing_failure_keeps_a_cure(monkeypatch):
    scratch, probes = _clone(monkeypatch, frozenset({"mesh_other"}))
    op = _FixEdges()
    verdict = scratch.session.try_op(op, op.targets_for(_SHORT), _SHORT.code, tolerance_mm=0.01,
                                     within=_REGION)
    assert verdict.kept and verdict.reason == session_mod.MESHES_AFTER_TARGET_REMAINS
    assert verdict.delta["mesh_cured"] == ["mesh_other"]
    assert probes[-1] is None   # proved on the whole delivered shape


def test_a_region_clone_without_it_still_rolls_back(monkeypatch):
    scratch, _ = _clone(monkeypatch)
    op = _FixEdges()
    verdict = scratch.session.try_op(op, op.targets_for(_SHORT), _SHORT.code, tolerance_mm=0.01,
                                     within=_REGION)
    assert not verdict.kept and verdict.reason == "target_remains"


def test_a_standing_no_answer_is_not_a_failure(monkeypatch):
    scratch, _ = _clone(monkeypatch, frozenset({"mesh_timeout"}))
    op = _FixEdges()
    verdict = scratch.session.try_op(op, op.targets_for(_SHORT), _SHORT.code, tolerance_mm=0.01,
                                     within=_REGION)
    assert not verdict.kept


def test_a_reset_clone_keeps_the_standing_failure(monkeypatch):
    scratch, _ = _clone(monkeypatch, frozenset({"mesh_other"}))
    scratch.reset()
    assert scratch.session._standing_mesh == frozenset({"mesh_other"})


def test_the_region_loop_tells_the_author_the_parts_mesh_failures(monkeypatch):
    from meshpipeline.cad.repair import region_loop

    told: list = []

    class _Author:
        def observe_mesh(self, codes):
            told.append(codes)

    region_loop._observe_shape(_Author(), "shape", None, (_SHORT, _FACETS, _TIMEOUT))
    assert told == [frozenset({"mesh_other"})]


def test_the_investigator_hands_its_clone_the_standing_failure():
    from meshpipeline.cad.repair import investigator

    seen: dict = {}

    def factory(shape, **kwargs):
        seen.update(kwargs)
        return "clone"

    assert investigator._scratch_of(factory, "shape", original=MEASURE, defects=(_SHORT,),
                                    reserved=frozenset(), remaining_s=lambda: 10.0,
                                    clock=lambda: 0.0, tolerance_mm=0.01,
                                    standing_mesh=frozenset({"mesh_other"})) == "clone"
    assert seen["standing_mesh"] == frozenset({"mesh_other"})


# THE CURE PROOF SPENDS ONLY WHAT IS LEFT (Greptile PRRT_kwDOUFq_Ps656SQR): the package's proof
# re-reads the clock after the region scan, and the STEP round-trip comes out of the probe's budget.


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _timed(monkeypatch, clock, *, apply_s=0.0, scan_s=0.0, export_s=0.0):
    budgets: list = []

    def scan(shape, **kw):
        if scans:
            clock.now += scan_s
        scans.append(kw.get("within"))
        return (_SHORT,), True
    scans: list = []
    monkeypatch.setattr(session_mod, "_localize_scan", scan)

    def probe(shape, **kw):
        budgets.append((kw.get("within"), kw.get("budget_s")))
        return (_FACETS,) if len(budgets) == 1 else ()
    monkeypatch.setattr(mesh_probe, "mesh_defects", probe)

    def export(shape):
        clock.now += export_s
        return shape
    monkeypatch.setattr(session_mod, "_as_delivered", export)

    class _Slow(_FixEdges):
        def apply(self, shape, targets, *, tolerance_mm):
            clock.now += apply_s
            return OpOutcome(shape="fixed")
    return _Slow(), budgets


def test_the_round_trip_is_paid_out_of_the_probes_budget(monkeypatch):
    clock = _Clock()
    op, budgets = _timed(monkeypatch, clock, export_s=3.0)
    session = RepairSession("shape", measurer=lambda s: MEASURE, clock=clock,
                            remaining_s=lambda: 100.0)
    target = session.defects()[0]
    assert session.try_op(op, op.targets_for(target), target.code, tolerance_mm=0.01,
                          within=_REGION).kept
    whole = [b for within, b in budgets if within is None][-1]
    assert whole == pytest.approx(session_mod.mesh_probe_budget_s(100.0) - 3.0)


def test_the_package_proof_rereads_the_clock_after_the_region_scan(monkeypatch):
    # 60 s package: the op spends 40 and its rescan 15 (55 spent, 5 left) - then the package's
    # region scan another 15. Nothing is left, so the package's cure proof must not run at all;
    # read off the stale 55 it would have been granted 5 s. The export is free here, so only the
    # clock re-read can stop it.
    clock = _Clock()
    op, budgets = _timed(monkeypatch, clock, apply_s=40.0, scan_s=15.0, export_s=0.0)
    session = RepairSession("shape", measurer=lambda s: MEASURE, clock=clock)
    session.defects()
    package = _package(op, _SHORT, {"edge:short"})
    package = TargetPackage(region=package.region, intent=package.intent, plan=package.plan,
                            predicate=package.predicate,
                            budget=PackageBudget(max_ops=1, timeout_s=60.0, model_calls=0,
                                                 deviation_mm=0.0))
    verdict = session.try_package(package)
    whole_probes = [b for within, b in budgets if within is None]
    # the first scan's probe, and the op's own cure proof - and NOT a package-level one
    assert len(whole_probes) == 2, budgets
    assert verdict.status != session_mod.COMMITTED
    assert session.shape == "shape"


# THE TOURNAMENT JUDGES CURES THE WAY THE INVESTIGATOR DOES (LEAD, for T19): its rehearsal clones
# are seeded with one region's defects too, so they carry the part's standing mesh failure.


def test_a_tournament_rehearsal_clone_carries_the_standing_failure():
    from meshpipeline.cad.repair import tournament

    seen: dict = {}

    def make_scratch(shape, **kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop after the clone is asked for")

    package = _package(_FixEdges(), _SHORT, {"edge:short"})
    cand = tournament.Candidate(source="rules", package=package,
                                plan_key=tournament.plan_key(package))
    tournament.rehearse(cand, session_shape="shape", original=MEASURE, defects=(_SHORT,),
                        region=_REGION, tolerance_mm=0.01, budget_s=30.0,
                        make_scratch=make_scratch, run_tier=lambda *a, **k: None,
                        measure=lambda *a, **k: None, scope=lambda d, *a, **k: d,
                        standing_mesh=frozenset({"mesh_other"}))
    assert seen.get("standing_mesh") == frozenset({"mesh_other"})


def test_the_tournament_author_forwards_the_parts_mesh_failures():
    from meshpipeline.cad.repair import tournament

    played: dict = {}

    def play(cands, **kw):
        played.update(kw)
        return ()

    author = tournament.tournament_author("shape", original=MEASURE, rehearse_with=play,
                                          tolerance_mm=0.01)
    author.observe_mesh(frozenset({"mesh_other"}))
    try:
        author(_REGION, (_SHORT,), ())
    except Exception:  # noqa: BLE001 - only the hand-off is under test
        pass
    if played:
        assert played["standing_mesh"] == frozenset({"mesh_other"})
    assert callable(getattr(author, "observe_mesh", None))


# A CURE CONSUMES THE STANDING FAILURE (Greptile PRRT_kwDOUFq_Ps6574yk, "Working repairs fail on
# replay"). After the clone keeps a cure, the part meshes: the real session strips its mesh
# failures then, so the clone must too. Otherwise a second edit is "cured" against a stale failure
# on the clone, the real session rolls it back, and a package claiming both targets fails - losing
# the first, working repair.

_SHORT2 = EntityDefect(code=DefectCode.small_edge, severity=DefectSeverity.warning,
                       entity_type="edge", entity_index=2, message="short",
                       fingerprint="edge:short2", location=dict(_BOX))
_REGION2 = Region(index=0, fingerprints=frozenset({"edge:short", "edge:short2"}),
                  bbox_min=(0.0, 0.0, 0.0), bbox_max=(10.0, 10.0, 10.0))


class _FixEdgesAgain(_FixEdges):
    def apply(self, shape, targets, *, tolerance_mm):
        return OpOutcome(shape=f"{shape}+")


def test_a_second_edit_after_a_cure_is_not_cured_on_a_stale_failure(monkeypatch):
    from meshpipeline.cad.repair import investigator_tools

    monkeypatch.setattr(investigator_tools, "_clone", lambda shape: shape)
    _seq(monkeypatch, session_mod, "_localize_scan", [((_SHORT, _SHORT2), True)])
    _seq(monkeypatch, mesh_probe, "mesh_defects", [()])
    scratch = investigator_tools.Scratch.of(
        "shape", original=MEASURE, defects=(_SHORT, _SHORT2), measurer=lambda s: MEASURE,
        standing_mesh=frozenset({"mesh_other"}))
    op = _FixEdgesAgain()
    first = scratch.session.try_op(op, op.targets_for(_SHORT), _SHORT.code, tolerance_mm=0.01,
                                   within=_REGION2)
    second = scratch.session.try_op(op, op.targets_for(_SHORT2), _SHORT2.code,
                                    tolerance_mm=0.01, within=_REGION2)
    assert first.kept and first.reason == session_mod.MESHES_AFTER_TARGET_REMAINS
    # The part already meshed before the second edit: nothing for it to cure.
    assert not second.kept and second.reason == "target_remains"
    # A reset reopens on the session's shape, where the part still failed: the seed comes back.
    scratch.reset()
    assert scratch.session._standing_mesh == frozenset({"mesh_other"})


def test_replay_of_what_the_clone_kept_commits_the_first_working_repair(monkeypatch):
    # The real session holds the whole list, so it sees the part's failure directly.
    _seq(monkeypatch, session_mod, "_localize_scan", [((_SHORT, _SHORT2), True)])
    _seq(monkeypatch, mesh_probe, "mesh_defects", [(_FACETS,), ()])
    real = RepairSession("shape", measurer=lambda s: MEASURE)
    real.defects()
    op = _FixEdgesAgain()
    # What a clone with the fix concludes: the first edit only (the second was not kept there).
    package = TargetPackage(
        region=_REGION2, intent="the working repair",
        plan=({"op": op, "targets": op.targets_for(_SHORT), "defect_code": _SHORT.code},),
        predicate=Predicate(cleared=frozenset({"edge:short"}), reclassified=frozenset(),
                            unchanged=frozenset(), max_area_delta_mm2=10.0,
                            max_volume_delta_mm3=10.0),
        budget=PackageBudget(max_ops=2, timeout_s=60.0, model_calls=0, deviation_mm=0.0))
    verdict = real.try_package(package)
    assert verdict.status == session_mod.COMMITTED, verdict.reason
    assert verdict.verdicts[0].reason == session_mod.MESHES_AFTER_TARGET_REMAINS
    assert real.shape == "shape+"
    # And the stale-cure plan the bug produced - both edits - is what the real session refuses.
    both = TargetPackage(
        region=_REGION2, intent="both", plan=(
            {"op": op, "targets": op.targets_for(_SHORT), "defect_code": _SHORT.code},
            {"op": op, "targets": op.targets_for(_SHORT2), "defect_code": _SHORT2.code}),
        predicate=Predicate(cleared=frozenset({"edge:short", "edge:short2"}),
                            reclassified=frozenset(), unchanged=frozenset(),
                            max_area_delta_mm2=10.0, max_volume_delta_mm3=10.0),
        budget=PackageBudget(max_ops=2, timeout_s=60.0, model_calls=0, deviation_mm=0.0))
    _seq(monkeypatch, session_mod, "_localize_scan", [((_SHORT, _SHORT2), True)])
    _seq(monkeypatch, mesh_probe, "mesh_defects", [(_FACETS,), ()])
    fresh = RepairSession("shape", measurer=lambda s: MEASURE)
    fresh.defects()
    refused = fresh.try_package(both)
    assert refused.verdicts[1].reason == "target_remains"
    assert refused.status != session_mod.COMMITTED
