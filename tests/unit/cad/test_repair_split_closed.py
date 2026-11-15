# Responsibility: Verify split_closed_faces' claim (a periodic-surface mesh failure on a face), its
#                 decline order, its exact face-count declaration, and that its flag keeps it out
#                 of the catalogue and out of the rules baseline's MESH_OPS when off.
# Boundaries: no kernel - the closedness check, the owner lookup, the divider and the splice are
#             monkeypatched. The real ShapeUpgrade_ShapeDivideClosed is proved in the native tier.
from __future__ import annotations

import pytest

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
    SplitClosedFaces,
    catalogue,
)
from meshpipeline.cad.repair.regions import Region
from meshpipeline.cad.repair.rules_author import MESH_OPS, mesh_ops, rules_author

_BOX = {"centroid": [5.0, 5.0, 5.0], "bbox_min": [0.0, 0.0, 0.0], "bbox_max": [10.0, 10.0, 10.0]}
REF = EntityRef(entity_type="face", fingerprint="face:cyl", entity_index=3)


def _periodic(*, stage: str = STAGE_2D, entity_type: str = "face",
              code: DefectCode = DefectCode.mesh_periodic_surface) -> EntityDefect:
    return EntityDefect(code=code, severity=DefectSeverity.error, entity_type=entity_type,
                        entity_index=3, message=code.value, fingerprint="face:cyl",
                        location=dict(_BOX),
                        measurements={"mesh_kind": "periodic_surface", "gmsh_surfaces": [3],
                                      "stage": stage})


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_SPLIT_CLOSED_FACES", True)


@pytest.fixture
def doubles(monkeypatch, flag_on):
    """Every kernel seam replaced. `configure` sets what each one answers."""
    calls: list[str] = []

    def configure(*, closed=(True, False), owner="SOLID", faces_before=3, faces_after=4):
        calls.clear()
        monkeypatch.setattr(operations, "sub_shape",
                            lambda shape, kind, index: "FACE" if index == 3 else None)
        monkeypatch.setattr(operations, "_closed_directions",
                            lambda face: calls.append("closed") or closed)
        owners = () if owner is None else owner if isinstance(owner, tuple) else (owner,)
        monkeypatch.setattr(operations, "_owners_of",
                            lambda shape, face: calls.append("owner") or owners)
        monkeypatch.setattr(operations, "_divide_closed",
                            lambda owner, *, tolerance_mm: calls.append("divide")
                            or ("DIVIDED", "CONTEXT"))
        counts = {"SOLID": faces_before, "DIVIDED": faces_after}
        monkeypatch.setattr(operations, "_face_count", lambda shape: counts[shape])
        monkeypatch.setattr(operations, "splice",
                            lambda shape, pairs: calls.append("splice") or ("SPLICED", "CTX"))
        monkeypatch.setattr(operations, "_split_fingerprints",
                            lambda owner, divided, **_: ("face:half1", "face:half2"))
        monkeypatch.setattr(operations, "_diagonal_of", lambda shape: 100.0)
        monkeypatch.setattr(operations, "mapping_from_context",
                            lambda *a, **k: operations.EntityMapping.empty())
        return SplitClosedFaces()

    configure.calls = calls
    return configure


# WHAT IT CLAIMS


def test_answers_and_targets(flag_on):
    op = SplitClosedFaces()
    assert op.name == "split_closed_faces"
    assert op.answers == frozenset({DefectCode.mesh_periodic_surface})
    assert op.scope == operations.FixSmallEdges.scope

    assert op.targets_for(_periodic()) == (REF,)
    # Only a located periodic failure on a face: another code, another entity type, or a defect
    # with no fingerprint is not this operation's to aim at.
    assert op.targets_for(_periodic(code=DefectCode.mesh_open_loop)) == ()
    assert op.targets_for(_periodic(entity_type="shape")) == ()
    assert op.targets_for(object()) == ()


def test_targets_nothing_while_the_flag_is_off(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_SPLIT_CLOSED_FACES", False)
    assert SplitClosedFaces().targets_for(_periodic()) == ()


# WHAT IT DECLINES


def test_non_periodic_face_declines(doubles):
    op = doubles(closed=(False, False))
    outcome = op.apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.shape == "SHAPE"
    assert outcome.evidence["declined"].startswith("not_periodic")
    assert AREA_DELTA not in outcome.evidence and VOLUME_DELTA not in outcome.evidence
    assert "divide" not in doubles.calls


def test_nothing_split_declines(doubles):
    # The divider ran and reported nothing divided: a no-op is a decline, not a kept operation.
    op = doubles(faces_after=3)
    outcome = op.apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.shape == "SHAPE"
    assert outcome.evidence["declined"].startswith("nothing_split")
    assert "splice" not in doubles.calls


@pytest.mark.parametrize("targets, reason", [
    ((), "no face was named"),
    ((REF, EntityRef(entity_type="face", fingerprint="face:other", entity_index=5)),
     "one_face_only"),
    ((EntityRef(entity_type="face", fingerprint="face:gone", entity_index=99),),
     "not_resolved"),
])
def test_declines_what_it_cannot_aim_at(doubles, targets, reason):
    op = doubles()
    outcome = op.apply("SHAPE", targets, tolerance_mm=0.01)
    assert outcome.shape == "SHAPE"
    assert outcome.evidence["declined"].startswith(reason)


def test_a_face_with_no_owning_body_declines(doubles):
    outcome = doubles(owner=None).apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.evidence["declined"].startswith("no_owner")


def test_a_face_two_bodies_share_declines(doubles):
    # Non-manifold: rebuilding one owner would leave the other holding the unsplit face.
    outcome = doubles(owner=("SOLID", "OTHER")).apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.shape == "SHAPE"
    assert outcome.evidence["declined"].startswith("non_manifold (2 bodies")
    assert "divide" not in doubles.calls


def test_declines_while_the_flag_is_off(doubles, monkeypatch):
    op = doubles()
    monkeypatch.setattr(rcfg, "CAD_REPAIR_SPLIT_CLOSED_FACES", False)
    outcome = op.apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.shape == "SHAPE"
    assert outcome.evidence["declined"] == "splitting_disabled (CAD_REPAIR_SPLIT_CLOSED_FACES)"
    assert doubles.calls == []


# WHAT IT DECLARES


def test_declares_exact_face_count(doubles):
    op = doubles(faces_before=3, faces_after=4)
    outcome = op.apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert "declined" not in outcome.evidence
    assert outcome.shape == "SPLICED"
    assert outcome.evidence["faces_added"] == 1
    # SPLITTING MOVES NO SURFACE: declared zero, explicitly.
    assert outcome.evidence[AREA_DELTA] == 0.0
    assert outcome.evidence[VOLUME_DELTA] == 0.0
    assert outcome.added == ("face:half1", "face:half2")
    assert doubles.calls == ["closed", "owner", "divide", "splice"]


def test_declares_every_face_the_divider_added(doubles):
    # The divider works on the whole owning solid, so a second closed face on it is split too -
    # and the declaration is the exact delta, not "one per target".
    outcome = doubles(faces_before=5, faces_after=7).apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.evidence["faces_added"] == 2


# THE FLAG KEEPS IT OUT OF EVERYTHING


def test_absent_from_the_catalogue_and_mesh_ops_when_off():
    # The import-time catalogue under the default (off) environment is today's, byte for byte.
    assert not rcfg.CAD_REPAIR_SPLIT_CLOSED_FACES
    assert "split_closed_faces" not in {op.name for op in OPERATIONS}
    assert DefectCode.mesh_periodic_surface not in MESH_OPS
    assert [op.name for op in catalogue(split_closed_faces=False)] == \
           [op.name for op in OPERATIONS]
    assert mesh_ops(split_closed_faces=False) == MESH_OPS


def test_present_in_the_catalogue_and_mesh_ops_when_on():
    names = [op.name for op in catalogue(split_closed_faces=True)]
    assert "split_closed_faces" in names
    assert names.count("split_closed_faces") == 1
    assert mesh_ops(split_closed_faces=True)[DefectCode.mesh_periodic_surface] == \
        "split_closed_faces"


def test_catalogue_reads_the_flag_when_not_told(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_SPLIT_CLOSED_FACES", True)
    assert "split_closed_faces" in {op.name for op in catalogue()}
    assert DefectCode.mesh_periodic_surface in mesh_ops()
    monkeypatch.setattr(rcfg, "CAD_REPAIR_SPLIT_CLOSED_FACES", False)
    assert "split_closed_faces" not in {op.name for op in catalogue()}
    assert DefectCode.mesh_periodic_surface not in mesh_ops()


# THE RULES BASELINE AIMS IT ONLY AT A SURFACE-STAGE FAILURE

MEASURE = Measure(valid=True, solids=1, shells=1, faces=20, edges=60, vertices=40,
                  diagonal_mm=200.0, max_tolerance_mm=0.01,
                  surface_area_mm2=1000.0, volume_mm3=8000.0)
_REGION = Region(index=0, fingerprints=frozenset({"face:cyl"}),
                 bbox_min=(0.0, 0.0, 0.0), bbox_max=(10.0, 10.0, 10.0),
                 face_indices=frozenset({3}))


@pytest.fixture
def baseline_on(monkeypatch, flag_on):
    monkeypatch.setattr(rules_author_module, "MESH_OPS", mesh_ops(split_closed_faces=True))
    monkeypatch.setattr(rules_author_module, "_OPS_BY_NAME",
                        {op.name: op for op in catalogue(split_closed_faces=True)})


def test_rules_author_aims_split_at_a_surface_stage_periodic_failure(baseline_on):
    package = rules_author(None, original=MEASURE)(_REGION, (_periodic(),), ())
    assert package is not None
    item = package.plan[0]
    assert item["op"].name == "split_closed_faces"
    assert [t.fingerprint for t in item["targets"]] == ["face:cyl"]
    assert package.predicate.cleared == frozenset({"face:cyl"})


@pytest.mark.parametrize("stage", [STAGE_3D, ""], ids=["volume_stage", "unknown_stage"])
def test_rules_author_refuses_a_periodic_failure_not_from_the_surface_stage(baseline_on, stage):
    # A periodic failure is raised by gmsh's 2D stage. Any other stage - including one nobody
    # recorded - gets no package: fail toward not acting.
    author = rules_author(None, original=MEASURE)
    assert author(_REGION, (_periodic(stage=stage),), ()) is None
    assert {r["reason"] for r in author.refusals} == {"no_operation_claims_it"}


def test_rules_author_does_not_propose_an_operation_missing_from_the_catalogue(monkeypatch):
    # MESH_OPS naming an operation the catalogue does not hold is refused, not a KeyError.
    monkeypatch.setattr(rules_author_module, "MESH_OPS", mesh_ops(split_closed_faces=True))
    author = rules_author(None, original=MEASURE)
    assert author(_REGION, (_periodic(),), ()) is None
    assert {r["reason"] for r in author.refusals} == {"no_operation_claims_it"}
