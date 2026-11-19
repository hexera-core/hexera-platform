# Responsibility: Verify close_wire_gaps' claim (an open loop recorded in the 2D stage, a kernel
#                 wire gap on a face), its decline order, its declarations (vertices moved, the
#                 measured largest move, the measured area change), its `rebuilt` names, that its
#                 flag keeps it out of the catalogue and the rules baseline, that the baseline
#                 prefers it for an open loop while still reaching fix_face_boundary, and that a
#                 wire gap still meets the narrower face fixer first.
# Boundaries: no kernel - the face lookup, the copy, the owner lookup, the gap measurement, the
#             closure, the validity checks, the splice, the area and the naming are monkeypatched.
#             The real ShapeFix_Wire passes are proved in
#             tests/native/test_native_repair_close_wire_gaps.py.
from __future__ import annotations

import logging

import pytest

import meshpipeline.cad.repair.mesh_probe as mesh_probe
import meshpipeline.cad.repair.operations as operations
import meshpipeline.cad.repair.rules_author as rules_author_module
import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.identity import EntityRef
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.mesh_probe import STAGE_2D, STAGE_3D
from meshpipeline.cad.repair.operations import (
    AREA_DELTA,
    OPERATIONS,
    VOLUME_DELTA,
    CloseWireGaps,
    FixFaceBoundary,
    catalogue,
)
from meshpipeline.cad.repair.plan import RulesPlanner, RunHistory
from meshpipeline.cad.repair.regions import Region
from meshpipeline.cad.repair.rules_author import (
    MESH_FALLBACKS,
    MESH_OPS,
    mesh_fallbacks,
    mesh_ops,
    rules_author,
)
from meshpipeline.cad.repair.session import OpVerdict, PackageVerdict

_BOX = {"centroid": [5.0, 5.0, 5.0], "bbox_min": [0.0, 0.0, 0.0], "bbox_max": [10.0, 10.0, 10.0]}
REF = EntityRef(entity_type="face", fingerprint="face:loop", entity_index=1)


def _open_loop(*, stage: str | None = STAGE_2D, entity_type: str = "face",
               code: DefectCode = DefectCode.mesh_open_loop) -> EntityDefect:
    measurements = {"mesh_kind": "open_loop", "gmsh_surfaces": [1]}
    if stage is not None:
        measurements["stage"] = stage
    return EntityDefect(code=code, severity=DefectSeverity.error, entity_type=entity_type,
                        entity_index=1, message="The 1D mesh seems not to be forming a closed loop",
                        fingerprint="face:loop", location=dict(_BOX), measurements=measurements)


def _wire_gap(*, entity_type: str = "face") -> EntityDefect:
    return EntityDefect(code=DefectCode.wire_gap, severity=DefectSeverity.error,
                        entity_type=entity_type, entity_index=1, message="wire gap",
                        fingerprint="face:loop", location=dict(_BOX), measurements={})


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_CLOSE_WIRE_GAPS", True)


@pytest.fixture
def doubles(monkeypatch, flag_on):
    """Every kernel seam replaced. `configure` sets what each one answers."""
    calls: list = []

    def configure(*, owner="SOLID", gap_mm=0.004, fixed=True, still_open=False,
                  moves=(0.002, 0.002), face_valid=True, owner_valid_before=True,
                  owner_valid_after=True, areas=None, names=None,
                  touched=("NEW_FACE", "NEIGHBOUR")):
        calls.clear()
        monkeypatch.setattr(operations, "sub_shape",
                            lambda shape, kind, index: f"FACE@{shape}" if index == 1 else None)
        monkeypatch.setattr(operations, "_copied", lambda shape: calls.append("copy") or "WORK")
        owners = () if owner is None else owner if isinstance(owner, tuple) else (owner,)
        monkeypatch.setattr(operations, "_owners_of",
                            lambda shape, face: calls.append(("owner", shape)) or owners)
        monkeypatch.setattr(operations, "_wire_gap_mm",
                            lambda face: calls.append(("gap", face)) or gap_mm)

        def close(owner, face, *, tolerance_mm):
            calls.append(("close", owner, face, tolerance_mm))
            return operations.WireClosure(owner="NEW_SOLID", face="NEW_FACE", fixed=fixed,
                                          still_open=still_open, moves=tuple(moves),
                                          touched=tuple(touched), max_tolerance_mm=0.002)

        monkeypatch.setattr(operations, "_close_gaps", close)
        validity = {"NEW_FACE": face_valid, "SOLID": owner_valid_before,
                    "NEW_SOLID": owner_valid_after}
        monkeypatch.setattr(operations, "_is_valid",
                            lambda shape: calls.append(("valid", shape)) or validity[shape])

        def splice(shape, pairs):
            calls.append(("splice", shape, tuple(pairs)))
            return ("SPLICED", "BODY_CTX")

        monkeypatch.setattr(operations, "splice", splice)
        measured = areas or {"SOLID": 600.0, "NEW_SOLID": 600.0125}
        monkeypatch.setattr(operations, "_total_face_area", lambda body: measured[body])
        named = names or {"NEW_FACE": "face:loop2", "NEIGHBOUR": "face:side2",
                          "FACE@WORK": "face:loop"}
        monkeypatch.setattr(operations, "_face_name", lambda face, *, diagonal_mm: named[face])
        monkeypatch.setattr(operations, "_diagonal_of", lambda shape: 100.0)
        monkeypatch.setattr(operations, "_recorded",
                            lambda old, new: calls.append(("recorded", old, new)) or "FACE_CTX")

        def mapping(context, targets, *, shape_before, diagonal_mm):
            calls.append(("mapping", context, shape_before))
            return operations.EntityMapping.empty()

        monkeypatch.setattr(operations, "mapping_from_context", mapping)
        return CloseWireGaps()

    configure.calls = calls
    return configure


def _called(calls, kind) -> list:
    return [c for c in calls if isinstance(c, tuple) and c[0] == kind]


# WHAT IT CLAIMS


def test_answers_an_open_loop_and_a_wire_gap(flag_on):
    op = CloseWireGaps()
    assert op.name == "close_wire_gaps"
    assert op.answers == frozenset({DefectCode.mesh_open_loop, DefectCode.wire_gap})
    assert op.targets_for(_open_loop()) == (REF,)
    assert op.targets_for(_wire_gap()) == (REF,)
    for code in DefectCode:
        if code not in op.answers:
            assert op.targets_for(_open_loop(code=code)) == (), code
    assert op.targets_for(_open_loop(entity_type="edge")) == ()
    assert op.targets_for(_wire_gap(entity_type="wire")) == ()
    assert op.targets_for(object()) == ()


@pytest.mark.parametrize("stage", [STAGE_3D, "", None, mesh_probe.STAGE_IMPORT],
                         ids=["volume", "empty", "unrecorded", "import"])
def test_only_a_surface_stage_open_loop_is_aimed_at(flag_on, stage):
    # The SplitClosedFaces rule: gmsh closes a face's 1D loop while meshing surfaces.
    assert CloseWireGaps().targets_for(_open_loop(stage=stage)) == ()


def test_a_wire_gap_needs_no_stage(flag_on):
    # A wire gap is a KERNEL code (BRepCheck), not a mesh failure: it has no stage to record.
    assert CloseWireGaps().targets_for(_wire_gap()) == (REF,)


def test_targets_nothing_while_the_flag_is_off(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_CLOSE_WIRE_GAPS", False)
    assert CloseWireGaps().targets_for(_open_loop()) == ()
    assert CloseWireGaps().targets_for(_wire_gap()) == ()


def test_its_scope_puts_the_narrower_face_fixer_first_for_a_wire_gap():
    # fix_face_boundary moves no vertex; it must get the wire gap before this op does.
    assert FixFaceBoundary.scope < CloseWireGaps.scope < operations.FixShapeGlobal.scope


def test_the_rules_planner_offers_a_wire_gap_to_fix_face_boundary_first(flag_on):
    planner = RulesPlanner(catalogue(close_wire_gaps=True))
    history = RunHistory()
    first = planner.propose([_wire_gap()], history, shape=None)
    assert first is not None and first.op.name == "fix_face_boundary"
    history.ineffective.add(("face:loop", "fix_face_boundary"))
    second = planner.propose([_wire_gap()], history, shape=None)
    assert second is not None and second.op.name == "close_wire_gaps"


# WHAT IT DECLARES


def test_declares_the_measured_moves_and_area(doubles):
    outcome = doubles().apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert "declined" not in outcome.evidence, outcome.evidence
    assert outcome.shape == "SPLICED"
    assert outcome.evidence["vertices_moved"] == 2
    assert outcome.evidence["max_vertex_move_mm"] == pytest.approx(0.002)
    assert outcome.evidence["gap_mm"] == pytest.approx(0.004)
    assert outcome.evidence["max_tolerance_mm"] == pytest.approx(0.002)
    # THE MEASURED AREA of the closed owner minus the original's; no volume declaration.
    assert outcome.evidence[AREA_DELTA] == pytest.approx(0.0125)
    assert VOLUME_DELTA not in outcome.evidence
    assert outcome.evidence["faces_touched"] == 2
    # The closure runs on the COPY's owner, at the run's tolerance, and the owner is spliced in.
    assert _called(doubles.calls, "close") == [("close", "SOLID", "FACE@WORK", 0.01)]
    assert _called(doubles.calls, "splice") == [("splice", "WORK", (("SOLID", "NEW_SOLID"),))]
    # The mapping reads the FACE's own history against the copy.
    assert ("recorded", "FACE@WORK", "NEW_FACE") in doubles.calls
    assert ("mapping", "FACE_CTX", "WORK") in doubles.calls


def test_rebuilt_names_the_closed_face_and_the_neighbours_sharing_moved_edges(doubles):
    outcome = doubles().apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.rebuilt == ("face:loop2", "face:side2")
    # Nothing was ADDED: a closure creates no face.
    assert outcome.added == ()


def test_a_closure_that_moved_no_vertex_declares_zero_moves(doubles):
    # FixGaps3d alone reshapes curve ends and moves no vertex: kept, and said to be so.
    outcome = doubles(moves=()).apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert "declined" not in outcome.evidence
    assert outcome.evidence["vertices_moved"] == 0
    assert outcome.evidence["max_vertex_move_mm"] == 0.0


# WHAT IT DECLINES


@pytest.mark.parametrize("targets, reason", [
    ((), "no face was named"),
    ((REF, EntityRef(entity_type="face", fingerprint="face:other", entity_index=2)),
     "one_face_only"),
    ((EntityRef(entity_type="face", fingerprint="face:gone", entity_index=99),),
     "not_resolved"),
])
def test_declines_what_it_cannot_aim_at(doubles, targets, reason):
    outcome = doubles().apply("SHAPE", targets, tolerance_mm=0.01)
    assert outcome.shape == "SHAPE"
    assert outcome.evidence["declined"].startswith(reason)
    assert "copy" not in doubles.calls


def test_a_face_owned_by_no_body_is_declined(doubles):
    outcome = doubles(owner=None).apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.evidence["declined"].startswith("no_owner")


def test_a_multi_owner_face_is_declined_as_non_manifold(doubles):
    outcome = doubles(owner=("SOLID", "OTHER")).apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.shape == "SHAPE"
    assert outcome.evidence["declined"].startswith("non_manifold")
    assert not _called(doubles.calls, "close")


def test_a_gap_wider_than_the_tolerance_is_declined_before_anything_moves(doubles):
    outcome = doubles(gap_mm=0.05).apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.shape == "SHAPE"
    assert outcome.evidence["declined"].startswith("gaps_exceed_tolerance")
    assert outcome.evidence["gap_mm"] == pytest.approx(0.05)
    assert not _called(doubles.calls, "close")
    assert AREA_DELTA not in outcome.evidence


def test_a_measured_move_beyond_the_tolerance_is_declined(doubles):
    # The gap read inside tolerance, but what the fixer ACTUALLY moved did not: the measurement
    # wins over the prediction.
    outcome = doubles(moves=(0.002, 0.03)).apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.shape == "SHAPE"
    assert outcome.evidence["declined"].startswith("gaps_exceed_tolerance")
    assert outcome.evidence["max_vertex_move_mm"] == pytest.approx(0.03)
    assert AREA_DELTA not in outcome.evidence
    assert not _called(doubles.calls, "splice")


def test_a_face_with_no_measurable_gap_is_declined_and_the_fixer_never_runs(doubles):
    outcome = doubles(gap_mm=0.0).apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.shape == "SHAPE"
    assert outcome.evidence["declined"].startswith("not_closed")
    assert not _called(doubles.calls, "close")


def test_a_fixer_that_fixed_nothing_is_declined_never_kept_as_a_no_op(doubles):
    outcome = doubles(fixed=False, moves=()).apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.shape == "SHAPE"
    assert outcome.evidence["declined"].startswith("not_closed")
    assert AREA_DELTA not in outcome.evidence
    assert not _called(doubles.calls, "splice")


def test_a_loop_still_open_after_the_fix_is_declined(doubles):
    outcome = doubles(still_open=True).apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.shape == "SHAPE"
    assert outcome.evidence["declined"].startswith("not_closed")


def test_a_closed_face_that_is_not_valid_is_declined(doubles):
    outcome = doubles(face_valid=False).apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.shape == "SHAPE"
    assert outcome.evidence["declined"].startswith("closed_invalid")
    assert not _called(doubles.calls, "splice")


def test_an_owner_that_would_lose_validity_is_declined(doubles):
    outcome = doubles(owner_valid_after=False).apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.shape == "SHAPE"
    assert outcome.evidence["declined"].startswith("owner_invalidated")
    assert AREA_DELTA not in outcome.evidence
    assert not _called(doubles.calls, "splice")


def test_an_owner_already_invalid_is_not_blamed_on_the_closure(doubles):
    # The usual case for a real open wire: the face, and so its owner, is invalid BEFORE.
    outcome = doubles(owner_valid_before=False, owner_valid_after=False).apply(
        "SHAPE", (REF,), tolerance_mm=0.01)
    assert "declined" not in outcome.evidence, outcome.evidence


def test_the_owner_is_judged_valid_before_the_closure_writes_into_its_edges(doubles):
    doubles().apply("SHAPE", (REF,), tolerance_mm=0.01)
    order = [c for c in doubles.calls if isinstance(c, tuple) and c[0] in ("valid", "close")]
    assert order.index(("valid", "SOLID")) < order.index(("close", "SOLID", "FACE@WORK", 0.01))


def test_declines_while_the_flag_is_off(doubles, monkeypatch):
    op = doubles()
    monkeypatch.setattr(rcfg, "CAD_REPAIR_CLOSE_WIRE_GAPS", False)
    outcome = op.apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.evidence["declined"] == "closing_disabled (CAD_REPAIR_CLOSE_WIRE_GAPS)"
    assert doubles.calls == []


# THE FLAG KEEPS IT OUT OF EVERYTHING


def test_absent_from_the_catalogue_and_mesh_ops_when_off():
    assert not rcfg.CAD_REPAIR_CLOSE_WIRE_GAPS
    assert "close_wire_gaps" not in {op.name for op in OPERATIONS}
    assert [op.name for op in catalogue(close_wire_gaps=False)] == [op.name for op in OPERATIONS]
    # FLAG OFF IS TODAY'S MAPPING, BYTE FOR BYTE, and no open-loop fallback chain exists.
    assert mesh_ops(close_wire_gaps=False) == MESH_OPS
    assert MESH_OPS[DefectCode.mesh_open_loop] == "fix_face_boundary"
    assert DefectCode.mesh_open_loop not in mesh_fallbacks(close_wire_gaps=False)
    assert DefectCode.mesh_open_loop not in MESH_FALLBACKS


def test_present_in_the_catalogue_and_preferred_in_mesh_ops_when_on():
    names = [op.name for op in catalogue(close_wire_gaps=True)]
    assert names.count("close_wire_gaps") == 1
    assert mesh_ops(close_wire_gaps=True)[DefectCode.mesh_open_loop] == "close_wire_gaps"
    assert mesh_fallbacks(close_wire_gaps=True) == {
        DefectCode.mesh_open_loop: ("fix_face_boundary",)}
    # The two fallback chains compose: neither flag hides the other's.
    assert mesh_fallbacks(close_wire_gaps=True, rebuild_face=True) == {
        DefectCode.mesh_open_loop: ("fix_face_boundary",),
        DefectCode.mesh_overlapping_facets: ("fix_shape_global",)}


def test_catalogue_reads_the_flag_when_not_told(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_CLOSE_WIRE_GAPS", True)
    assert "close_wire_gaps" in {op.name for op in catalogue()}
    assert mesh_ops()[DefectCode.mesh_open_loop] == "close_wire_gaps"
    monkeypatch.setattr(rcfg, "CAD_REPAIR_CLOSE_WIRE_GAPS", False)
    assert "close_wire_gaps" not in {op.name for op in catalogue()}
    assert mesh_ops()[DefectCode.mesh_open_loop] == "fix_face_boundary"


# THE GATE IS SAID OUT LOUD


@pytest.mark.parametrize("closing, review, warns", [
    (True, "off", True), (True, "live", False), (False, "off", False), (False, "live", False),
])
def test_closing_without_the_package_review_is_warned(caplog, closing, review, warns):
    with caplog.at_level(logging.WARNING, logger=rcfg.logger.name):
        rcfg._warn_unreviewed_closure(closing, review)
    assert any("CAD_REPAIR_CLOSE_WIRE_GAPS" in r.getMessage() for r in caplog.records) is warns


# THE RULES BASELINE PREFERS IT, AND STILL REACHES fix_face_boundary

MEASURE = Measure(valid=True, solids=1, shells=1, faces=8, edges=27, vertices=54,
                  diagonal_mm=132.0, max_tolerance_mm=0.01,
                  surface_area_mm2=20000.0, volume_mm3=100000.0)
_REGION = Region(index=0, fingerprints=frozenset({"face:loop"}),
                 bbox_min=(0.0, 0.0, 0.0), bbox_max=(10.0, 10.0, 10.0),
                 face_indices=frozenset({1}))


@pytest.fixture
def baseline_on(monkeypatch, flag_on):
    monkeypatch.setattr(rules_author_module, "MESH_OPS", mesh_ops(close_wire_gaps=True))
    monkeypatch.setattr(rules_author_module, "MESH_FALLBACKS",
                        mesh_fallbacks(close_wire_gaps=True))
    monkeypatch.setattr(rules_author_module, "_OPS_BY_NAME",
                        {op.name: op for op in catalogue(close_wire_gaps=True)})


def _tried(op: str, *, declined: bool = False) -> PackageVerdict:
    return PackageVerdict(region_index=0, status="reverted", verdicts=(OpVerdict(
        op=op, targets=("face:loop",), defect_code="mesh_open_loop",
        status="rolled_back", reason="declined:x" if declined else "target_remains",
        evidence={"declined": "not_closed"} if declined else {}),))


def test_rules_author_prefers_closure_for_a_surface_stage_open_loop(baseline_on):
    package = rules_author(None, original=MEASURE)(_REGION, (_open_loop(),), ())
    assert package is not None
    assert package.plan[0]["op"].name == "close_wire_gaps"
    assert [t.fingerprint for t in package.plan[0]["targets"]] == ["face:loop"]


@pytest.mark.parametrize("declined", [False, True], ids=["ineffective", "declined"])
def test_the_fallback_is_reached_once_closure_was_tried(baseline_on, declined):
    author = rules_author(None, original=MEASURE)
    package = author(_REGION, (_open_loop(),), (_tried("close_wire_gaps", declined=declined),))
    assert package is not None
    assert package.plan[0]["op"].name == "fix_face_boundary"
    assert author(_REGION, (_open_loop(),), (_tried("close_wire_gaps", declined=declined),
                                             _tried("fix_face_boundary"))) is None


def test_an_unrecorded_stage_open_loop_skips_closure_and_keeps_todays_choice(baseline_on):
    package = rules_author(None, original=MEASURE)(_REGION, (_open_loop(stage=None),), ())
    assert package is not None and package.plan[0]["op"].name == "fix_face_boundary"


def test_with_the_flag_off_the_open_loop_goes_to_fix_face_boundary():
    package = rules_author(None, original=MEASURE)(_REGION, (_open_loop(),), ())
    assert package is not None and package.plan[0]["op"].name == "fix_face_boundary"
    assert package.rationale == ("regions-rules-v1: fix_face_boundary is the operation MESH_OPS "
                                 "aims at a mesh_open_loop")


def test_a_fallback_says_it_is_one_in_its_rationale(baseline_on):
    author = rules_author(None, original=MEASURE)
    fallback = author(_REGION, (_open_loop(),), (_tried("close_wire_gaps"),))
    assert fallback.rationale.endswith("fix_face_boundary is the fallback for a mesh_open_loop "
                                       "after close_wire_gaps was spent on it")
