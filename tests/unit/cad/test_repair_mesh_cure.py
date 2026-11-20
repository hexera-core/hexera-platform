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
