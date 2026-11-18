# Responsibility: Verify rebuild_face's claim (an overlapping-facets mesh failure recorded in the 2D
#                 stage), its decline order, its exact declarations (one face out, one face in,
#                 measured area, sampled deviation), its `rebuilt` names, that its flag keeps it out
#                 of the catalogue and the rules baseline, and that the baseline prefers it to
#                 fix_shape_global while still reaching fix_shape_global once it was tried.
# Boundaries: no kernel - the face lookup, the copy, the owner lookup, the re-trim, the validity
#             checks, the splice, the area, the naming and the deviation are monkeypatched. The real
#             BRepBuilderAPI_MakeFace + ShapeFix_Face is proved in
#             tests/native/test_native_repair_rebuild_face.py.
from __future__ import annotations

import logging

import pytest

import meshpipeline.cad.repair.mesh_probe as mesh_probe
import meshpipeline.cad.repair.operations as operations
import meshpipeline.cad.repair.rules_author as rules_author_module
import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.deviation import DeviationResult
from meshpipeline.cad.repair.identity import EntityRef
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.mesh_probe import STAGE_2D, STAGE_3D
from meshpipeline.cad.repair.operations import (
    AREA_DELTA,
    OPERATIONS,
    VOLUME_DELTA,
    RebuildFace,
    catalogue,
)
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
REF = EntityRef(entity_type="face", fingerprint="face:cone", entity_index=1)


def _overlap(*, stage: str | None = STAGE_2D, entity_type: str = "face",
             code: DefectCode = DefectCode.mesh_overlapping_facets) -> EntityDefect:
    measurements = {"mesh_kind": "overlapping_facets", "gmsh_surfaces": [1]}
    if stage is not None:
        measurements["stage"] = stage
    return EntityDefect(code=code, severity=DefectSeverity.error, entity_type=entity_type,
                        entity_index=1, message="Invalid boundary mesh (overlapping facets)",
                        fingerprint="face:cone", location=dict(_BOX), measurements=measurements)


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REBUILD_FACE", True)


@pytest.fixture
def doubles(monkeypatch, flag_on):
    """Every kernel seam replaced. `configure` sets what each one answers."""
    calls: list = []

    def configure(*, owner="SOLID", retrimmed=("OLD", "NEW"), face_valid=True,
                  owner_valid_before=True, owner_valid_after=True,
                  areas=None, names=None, deviation=DeviationResult(0.004, 32, 1, 0, 0.01),
                  owner_faces=None):
        calls.clear()
        monkeypatch.setattr(operations, "sub_shape",
                            lambda shape, kind, index: f"FACE@{shape}" if index == 1 else None)
        monkeypatch.setattr(operations, "_copied", lambda shape: calls.append("copy") or "WORK")
        owners = () if owner is None else owner if isinstance(owner, tuple) else (owner,)
        monkeypatch.setattr(operations, "_owners_of",
                            lambda shape, face: calls.append(("owner", shape)) or owners)
        monkeypatch.setattr(operations, "_retrim_face",
                            lambda face, *, tolerance_mm: calls.append(("retrim", face))
                            or retrimmed)
        validity = {"NEW": face_valid, "SOLID": owner_valid_before,
                    "NEW_SOLID": owner_valid_after}
        monkeypatch.setattr(operations, "_is_valid",
                            lambda shape: calls.append(("valid", shape)) or validity[shape])

        def splice(shape, pairs):
            calls.append(("splice", shape, tuple(pairs)))
            return ("NEW_SOLID", "FACE_CTX") if shape == "SOLID" else ("SPLICED", "BODY_CTX")

        monkeypatch.setattr(operations, "splice", splice)
        measured = areas or {"OLD": 100.0, "NEW": 100.25}
        monkeypatch.setattr(operations, "_area_of", lambda face: measured[face])
        named = names or {"OLD": "face:cone", "NEW": "face:cone2"}
        monkeypatch.setattr(operations, "_face_name", lambda face, *, diagonal_mm: named[face])
        bodies = owner_faces or {"SOLID": ("face:cone", "face:top"),
                                 "NEW_SOLID": ("face:cone2", "face:top")}
        monkeypatch.setattr(operations, "_face_names", lambda body, **_: bodies[body])
        monkeypatch.setattr(operations, "_diagonal_of", lambda shape: 100.0)
        monkeypatch.setattr(operations, "_rebuild_deviation",
                            lambda old, new, name, *, diagonal_mm:
                            calls.append(("deviation", old, new, name)) or deviation)

        def mapping(context, targets, *, shape_before, diagonal_mm):
            calls.append(("mapping", context, shape_before))
            return operations.EntityMapping.empty()

        monkeypatch.setattr(operations, "mapping_from_context", mapping)
        return RebuildFace()

    configure.calls = calls
    return configure


# WHAT IT CLAIMS


def test_only_overlapping_facets(flag_on):
    op = RebuildFace()
    assert op.name == "rebuild_face"
    assert op.answers == frozenset({DefectCode.mesh_overlapping_facets})
    assert op.targets_for(_overlap()) == (REF,)
    for code in DefectCode:
        if code is not DefectCode.mesh_overlapping_facets:
            assert op.targets_for(_overlap(code=code)) == (), code
    assert op.targets_for(_overlap(entity_type="shape")) == ()
    assert op.targets_for(object()) == ()


@pytest.mark.parametrize("stage", [STAGE_3D, "", None, mesh_probe.STAGE_IMPORT],
                         ids=["volume", "empty", "unrecorded", "import"])
def test_only_a_surface_stage_overlap_is_aimed_at(flag_on, stage):
    # The SplitClosedFaces rule: a 3D-stage overlap is the volume mesher reading a surface mesh,
    # not a face whose trimming defeated the surface mesher - this op does not read it.
    assert RebuildFace().targets_for(_overlap(stage=stage)) == ()


def test_targets_nothing_while_the_flag_is_off(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REBUILD_FACE", False)
    assert RebuildFace().targets_for(_overlap()) == ()


# WHAT IT DECLARES


def test_declares_one_face_swap(doubles):
    outcome = doubles().apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert "declined" not in outcome.evidence, outcome.evidence
    assert outcome.shape == "SPLICED"
    assert outcome.evidence["faces_removed"] == 1
    assert outcome.evidence["faces_added"] == 1
    # THE MEASURED AREA of the new face minus the old, declared; no volume declaration.
    assert outcome.evidence[AREA_DELTA] == pytest.approx(0.25)
    assert VOLUME_DELTA not in outcome.evidence
    # The face is spliced into its OWNER, and the owner into the part - on the COPY.
    splices = [c for c in doubles.calls if isinstance(c, tuple) and c[0] == "splice"]
    assert splices == [("splice", "SOLID", (("OLD", "NEW"),)),
                       ("splice", "WORK", (("SOLID", "NEW_SOLID"),))]
    assert ("retrim", "FACE@WORK") in doubles.calls
    # The mapping reads the FACE-level history against the copy (whose indices are the input's).
    assert ("mapping", "FACE_CTX", "WORK") in doubles.calls
    assert outcome.added == ("face:cone2",)


def test_rebuild_records_deviation(doubles):
    outcome = doubles().apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.evidence["deviation_mm"] == pytest.approx(0.004)
    assert outcome.evidence["deviation_samples"] == 32
    # Measured from the NEW face, named by its own fingerprint, against the OLD face.
    assert ("deviation", "OLD", "NEW", "face:cone2") in doubles.calls


def test_an_unmeasured_deviation_is_recorded_as_none_never_a_decline(doubles):
    outcome = doubles(deviation=DeviationResult()).apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert "declined" not in outcome.evidence
    assert outcome.evidence["deviation_mm"] is None


def test_rebuilt_names_the_new_face_and_any_owner_face_the_reshape_renamed(doubles):
    op = doubles(owner_faces={"SOLID": ("face:cone", "face:top", "face:side"),
                              "NEW_SOLID": ("face:cone2", "face:top", "face:side2")})
    outcome = op.apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.rebuilt == ("face:cone2", "face:side2")


def test_an_unrenamed_face_adds_no_name_but_is_still_rebuilt(doubles):
    # A re-trim that leaves the face's box where it was: no new name, but the face is the op's.
    op = doubles(names={"OLD": "face:cone", "NEW": "face:cone"},
                 owner_faces={"SOLID": ("face:cone", "face:top"),
                              "NEW_SOLID": ("face:cone", "face:top")})
    outcome = op.apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.added == ()
    assert outcome.rebuilt == ("face:cone",)


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
    assert not any(isinstance(c, tuple) and c[0] == "retrim" for c in doubles.calls)


def test_no_recoverable_surface_or_wire_is_declined(doubles):
    outcome = doubles(retrimmed=None).apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.shape == "SHAPE"
    assert outcome.evidence["declined"].startswith("no_boundary")
    assert not any(isinstance(c, tuple) and c[0] == "splice" for c in doubles.calls)


def test_a_rebuilt_face_that_is_not_valid_is_declined(doubles):
    outcome = doubles(face_valid=False).apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.shape == "SHAPE"
    assert outcome.evidence["declined"].startswith("rebuilt_invalid")
    assert AREA_DELTA not in outcome.evidence
    assert not any(isinstance(c, tuple) and c[0] == "splice" for c in doubles.calls)


def test_an_owner_that_would_lose_validity_is_declined(doubles):
    outcome = doubles(owner_valid_after=False).apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.shape == "SHAPE"
    assert outcome.evidence["declined"].startswith("owner_invalidated")
    assert AREA_DELTA not in outcome.evidence
    assert "faces_removed" not in outcome.evidence
    # The owner was never spliced into the part.
    assert ("splice", "WORK", (("SOLID", "NEW_SOLID"),)) not in doubles.calls


def test_an_owner_already_invalid_is_not_blamed_on_the_rebuild(doubles):
    outcome = doubles(owner_valid_before=False, owner_valid_after=False).apply(
        "SHAPE", (REF,), tolerance_mm=0.01)
    assert "declined" not in outcome.evidence, outcome.evidence


def test_declines_while_the_flag_is_off(doubles, monkeypatch):
    op = doubles()
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REBUILD_FACE", False)
    outcome = op.apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.evidence["declined"] == "rebuilding_disabled (CAD_REPAIR_REBUILD_FACE)"
    assert doubles.calls == []


# THE FLAG KEEPS IT OUT OF EVERYTHING


def test_absent_from_the_catalogue_and_mesh_ops_when_off():
    assert not rcfg.CAD_REPAIR_REBUILD_FACE
    assert "rebuild_face" not in {op.name for op in OPERATIONS}
    assert [op.name for op in catalogue(rebuild_face=False)] == [op.name for op in OPERATIONS]
    # FLAG OFF IS TODAY'S MAPPING, BYTE FOR BYTE, and no fallback chain exists.
    assert mesh_ops(rebuild_face=False) == MESH_OPS
    assert MESH_OPS[DefectCode.mesh_overlapping_facets] == "fix_shape_global"
    assert mesh_fallbacks(rebuild_face=False) == MESH_FALLBACKS == {}


def test_present_in_the_catalogue_and_preferred_in_mesh_ops_when_on():
    names = [op.name for op in catalogue(rebuild_face=True)]
    assert names.count("rebuild_face") == 1
    assert mesh_ops(rebuild_face=True)[DefectCode.mesh_overlapping_facets] == "rebuild_face"
    assert mesh_fallbacks(rebuild_face=True) == {
        DefectCode.mesh_overlapping_facets: ("fix_shape_global",)}


def test_catalogue_reads_the_flag_when_not_told(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REBUILD_FACE", True)
    assert "rebuild_face" in {op.name for op in catalogue()}
    assert mesh_ops()[DefectCode.mesh_overlapping_facets] == "rebuild_face"
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REBUILD_FACE", False)
    assert "rebuild_face" not in {op.name for op in catalogue()}
    assert mesh_ops()[DefectCode.mesh_overlapping_facets] == "fix_shape_global"


# THE GATE IS SAID OUT LOUD


@pytest.mark.parametrize("rebuild, review, warns", [
    (True, "off", True), (True, "live", False), (False, "off", False), (False, "live", False),
])
def test_rebuild_without_the_package_review_is_warned(caplog, rebuild, review, warns):
    with caplog.at_level(logging.WARNING, logger=rcfg.logger.name):
        rcfg._warn_unreviewed_rebuild(rebuild, review)
    assert any("CAD_REPAIR_REBUILD_FACE" in r.getMessage() for r in caplog.records) is warns


# THE RULES BASELINE PREFERS IT, AND STILL REACHES fix_shape_global

MEASURE = Measure(valid=True, solids=1, shells=1, faces=8, edges=27, vertices=54,
                  diagonal_mm=132.0, max_tolerance_mm=0.01,
                  surface_area_mm2=20000.0, volume_mm3=100000.0)
_REGION = Region(index=0, fingerprints=frozenset({"face:cone"}),
                 bbox_min=(0.0, 0.0, 0.0), bbox_max=(10.0, 10.0, 10.0),
                 face_indices=frozenset({1}))


@pytest.fixture
def baseline_on(monkeypatch, flag_on):
    monkeypatch.setattr(rules_author_module, "MESH_OPS", mesh_ops(rebuild_face=True))
    monkeypatch.setattr(rules_author_module, "MESH_FALLBACKS", mesh_fallbacks(rebuild_face=True))
    monkeypatch.setattr(rules_author_module, "_OPS_BY_NAME",
                        {op.name: op for op in catalogue(rebuild_face=True)})


def _tried(op: str, *, declined: bool = False) -> PackageVerdict:
    return PackageVerdict(region_index=0, status="reverted", verdicts=(OpVerdict(
        op=op, targets=("face:cone",), defect_code="mesh_overlapping_facets",
        status="rolled_back", reason="declined:x" if declined else "target_remains",
        evidence={"declined": "no_boundary"} if declined else {}),))


def test_rules_author_prefers_rebuild_for_a_surface_stage_overlap(baseline_on):
    package = rules_author(None, original=MEASURE)(_REGION, (_overlap(),), ())
    assert package is not None
    assert package.plan[0]["op"].name == "rebuild_face"
    assert [t.fingerprint for t in package.plan[0]["targets"]] == ["face:cone"]


@pytest.mark.parametrize("declined", [False, True], ids=["ineffective", "declined"])
def test_the_fallback_is_reached_once_rebuild_was_tried(baseline_on, declined):
    author = rules_author(None, original=MEASURE)
    package = author(_REGION, (_overlap(),), (_tried("rebuild_face", declined=declined),))
    assert package is not None
    assert package.plan[0]["op"].name == "fix_shape_global"
    # And once BOTH were tried, the rules run dry.
    assert author(_REGION, (_overlap(),), (_tried("rebuild_face", declined=declined),
                                           _tried("fix_shape_global"))) is None


def test_an_unrecorded_stage_overlap_skips_rebuild_and_keeps_todays_choice(baseline_on):
    # rebuild_face refuses the aim itself (its targets_for), so the chain passes it over and the
    # overlap gets exactly the flag-off proposal.
    package = rules_author(None, original=MEASURE)(_REGION, (_overlap(stage=None),), ())
    assert package is not None and package.plan[0]["op"].name == "fix_shape_global"


def test_a_volume_stage_overlap_still_gets_no_package(baseline_on):
    author = rules_author(None, original=MEASURE)
    assert author(_REGION, (_overlap(stage=STAGE_3D),), ()) is None
    assert {r["reason"] for r in author.refusals} == {"no_operation_claims_it"}


def test_with_the_flag_off_the_overlap_goes_to_fix_shape_global():
    package = rules_author(None, original=MEASURE)(_REGION, (_overlap(),), ())
    assert package is not None and package.plan[0]["op"].name == "fix_shape_global"


def test_the_owner_is_judged_valid_before_the_retrim_writes_into_its_edges(doubles):
    doubles().apply("SHAPE", (REF,), tolerance_mm=0.01)
    order = [c for c in doubles.calls if isinstance(c, tuple) and c[0] in ("valid", "retrim")]
    assert order.index(("valid", "SOLID")) < order.index(("retrim", "FACE@WORK"))


def test_a_fallback_says_it_is_one_in_its_rationale(baseline_on):
    author = rules_author(None, original=MEASURE)
    first = author(_REGION, (_overlap(),), ())
    assert first.rationale.endswith("rebuild_face is the operation MESH_OPS aims at a "
                                    "mesh_overlapping_facets")
    fallback = author(_REGION, (_overlap(),), (_tried("rebuild_face"),))
    assert fallback.rationale.endswith("fix_shape_global is the fallback for a "
                                       "mesh_overlapping_facets after rebuild_face was spent on it")


def test_flag_off_rationale_is_unchanged():
    package = rules_author(None, original=MEASURE)(_REGION, (_overlap(),), ())
    assert package.rationale == ("regions-rules-v1: fix_shape_global is the operation MESH_OPS "
                                 "aims at a mesh_overlapping_facets")


@pytest.mark.parametrize("flag, code, op_name", [
    ("CAD_REPAIR_SPLIT_CLOSED_FACES", DefectCode.mesh_periodic_surface, "split_closed_faces"),
    ("CAD_REPAIR_FRAGMENT_BODIES", DefectCode.mesh_edge_through_face, "fragment_bodies"),
], ids=["split", "fragment"])
def test_a_claiming_op_that_refuses_a_non_face_aim_is_refused_up_front(monkeypatch, flag, code,
                                                                       op_name):
    # The chain passes over an op whose own targets_for refuses the aim: a periodic or
    # edge-through-face failure on an EDGE is refused by the author instead of handed to an op
    # that would only decline it.
    monkeypatch.setattr(rcfg, flag, True)
    monkeypatch.setattr(rules_author_module, "MESH_OPS", mesh_ops())
    monkeypatch.setattr(rules_author_module, "_OPS_BY_NAME", {op.name: op for op in catalogue()})
    stage = STAGE_3D if code is DefectCode.mesh_edge_through_face else STAGE_2D
    defect = EntityDefect(code=code, severity=DefectSeverity.error, entity_type="edge",
                          entity_index=1, message=code.value, fingerprint="face:cone",
                          location=dict(_BOX), measurements={"stage": stage})
    author = rules_author(None, original=MEASURE)
    assert author(_REGION, (defect,), ()) is None
    assert {r["reason"] for r in author.refusals} == {"no_operation_claims_it"}
    # The same failure on a FACE is still aimed at.
    face = EntityDefect(code=code, severity=DefectSeverity.error, entity_type="face",
                        entity_index=1, message=code.value, fingerprint="face:cone",
                        location=dict(_BOX), measurements={"stage": stage})
    package = rules_author(None, original=MEASURE)(_REGION, (face,), ())
    assert package is not None and package.plan[0]["op"].name == op_name
