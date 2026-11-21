# Responsibility: Verify resolve_sub_mesh_gap's claim (a gap-located or unmeasured clash, never a
#                 clash measured at 0 mm), its consent flag in the catalogue, the model's words and
#                 the rules baseline, the baseline's preference for it with the code's own choice
#                 as fallback, and the away-from-zero rounding of its declarations.
# Boundaries: no kernel. The split itself, its declines and the delivered STEP meshing are proved
#             in tests/native/test_native_repair_resolve_sub_mesh_gap.py.
from __future__ import annotations

import pytest

import meshpipeline.cad.repair.rules_author as rules_author_module
import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.agents.repair_review import _wire
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.gap_split import NAME, ResolveSubMeshGap, _rounded_up
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.mesh_probe import LOCATED_BY_CLASH, STAGE_2D, STAGE_3D
from meshpipeline.cad.repair.operations import catalogue
from meshpipeline.cad.repair.regions import Region
from meshpipeline.cad.repair.rules_author import mesh_fallbacks, mesh_ops, rules_author
from meshpipeline.cad.repair.session import OpVerdict, PackageVerdict

_BOX = {"centroid": [5.0, 5.0, 5.0], "bbox_min": [0.0, 0.0, 0.0], "bbox_max": [10.0, 10.0, 10.0]}


def _clash(*, gap: float | None = 0.02, code: DefectCode = DefectCode.mesh_edge_through_face,
           stage: str = STAGE_3D, entity_type: str = "face") -> EntityDefect:
    measurements: dict = {"mesh_kind": "edge_through_face", "stage": stage,
                          "gmsh_surfaces": []}
    if gap is not None:
        measurements.update(located_by=LOCATED_BY_CLASH, clash_gap_mm=gap, clash_pair=[1, 4])
    return EntityDefect(code=code, severity=DefectSeverity.error, entity_type=entity_type,
                        entity_index=1, message="An edge intersects a facet",
                        fingerprint="face:wall", location=dict(_BOX), measurements=measurements)


@pytest.fixture
def gap_on(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_RESOLVE_SUB_MESH_GAP", True)


# THE CLAIM

def test_the_flag_off_claims_nothing():
    assert ResolveSubMeshGap().targets_for(_clash()) == ()


@pytest.mark.parametrize("code", [DefectCode.mesh_edge_through_face,
                                  DefectCode.mesh_overlapping_facets])
def test_a_clash_measured_as_a_gap_is_claimed(gap_on, code):
    (ref,) = ResolveSubMeshGap().targets_for(_clash(code=code))
    assert (ref.entity_type, ref.fingerprint, ref.entity_index) == ("face", "face:wall", 1)


def test_an_unmeasured_clash_is_claimed_and_measured_by_the_operation(gap_on):
    assert len(ResolveSubMeshGap().targets_for(_clash(gap=None))) == 1


@pytest.mark.parametrize("gap", [0.0, -1.0, "x", float("nan")])
def test_faces_that_meet_are_a_real_intersection_and_never_claimed(gap_on, gap):
    assert ResolveSubMeshGap().targets_for(_clash(gap=gap)) == ()


def test_other_codes_and_non_faces_are_not_claimed(gap_on):
    op = ResolveSubMeshGap()
    assert op.targets_for(_clash(code=DefectCode.mesh_open_loop)) == ()
    assert op.targets_for(_clash(entity_type="shape")) == ()


# THE CONSENT FLAG, ON EVERY SIDE

@pytest.mark.parametrize("flag", [False, True], ids=["gap_off", "gap_on"])
def test_the_flag_keeps_it_in_or_out_of_every_catalogue(monkeypatch, flag):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_RESOLVE_SUB_MESH_GAP", flag)
    registry = {op.name for op in catalogue()}
    assert (NAME in registry) is flag
    assert set(_wire.catalogue()) == registry
    described = set(dict(line[2:].split(": ", 1)
                         for line in _wire.catalogue_text().splitlines()))
    assert described == registry
    told = {op.name for op in catalogue(resolve_sub_mesh_gap=flag)}
    assert told == registry


def test_flag_off_is_the_prior_catalogue_byte_for_byte(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_RESOLVE_SUB_MESH_GAP", False)
    assert [op.name for op in catalogue()] == [op.name for op in
                                              catalogue(resolve_sub_mesh_gap=False)]
    assert NAME not in _wire.catalogue_text()


# THE RULES BASELINE

MEASURE = Measure(valid=True, solids=1, shells=1, faces=4, edges=6, vertices=4,
                  diagonal_mm=142.8, max_tolerance_mm=1e-7,
                  surface_area_mm2=13000.0, volume_mm3=125000.0)
_REGION = Region(index=0, fingerprints=frozenset({"face:wall"}),
                 bbox_min=(0.0, 0.0, 0.0), bbox_max=(10.0, 10.0, 10.0),
                 face_indices=frozenset({1}))


@pytest.fixture
def baseline_on(monkeypatch, gap_on):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FRAGMENT_BODIES", True)
    monkeypatch.setattr(rules_author_module, "MESH_OPS", mesh_ops(fragment_bodies=True))
    monkeypatch.setattr(rules_author_module, "MESH_FALLBACKS", mesh_fallbacks())
    monkeypatch.setattr(rules_author_module, "_OPS_BY_NAME",
                        {op.name: op for op in catalogue(fragment_bodies=True,
                                                         resolve_sub_mesh_gap=True)})


def _tried(op: str, *, declined: bool = False) -> PackageVerdict:
    return PackageVerdict(region_index=0, status="reverted", verdicts=(OpVerdict(
        op=op, targets=("face:wall",), defect_code="mesh_edge_through_face",
        status="rolled_back", reason="declined:x" if declined else "target_remains",
        evidence={"declined": "no_sub_mesh_gap"} if declined else {}),))


def test_the_baseline_prefers_the_gap_operation_for_a_gap(baseline_on):
    package = rules_author(None, original=MEASURE)(_REGION, (_clash(),), ())
    assert package is not None
    assert package.plan[0]["op"].name == NAME
    assert [t.fingerprint for t in package.plan[0]["targets"]] == ["face:wall"]
    assert "do not touch" in package.rationale


@pytest.mark.parametrize("declined", [False, True], ids=["ineffective", "declined"])
def test_once_spent_the_code_gets_its_own_choice_next(baseline_on, declined):
    package = rules_author(None, original=MEASURE)(
        _REGION, (_clash(),), (_tried(NAME, declined=declined),))
    assert package is not None and package.plan[0]["op"].name == "fragment_bodies"


def test_a_volume_stage_overlap_on_a_gap_is_not_refused_for_its_stage(baseline_on):
    defect = _clash(code=DefectCode.mesh_overlapping_facets, gap=None, stage=STAGE_3D)
    package = rules_author(None, original=MEASURE)(_REGION, (defect,), ())
    assert package is not None and package.plan[0]["op"].name == NAME


def test_a_clash_at_zero_never_reaches_the_gap_operation(baseline_on):
    package = rules_author(None, original=MEASURE)(_REGION, (_clash(gap=0.0),), ())
    assert package is None or package.plan[0]["op"].name != NAME


def test_with_the_flag_off_the_baseline_is_unchanged(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_RESOLVE_SUB_MESH_GAP", False)
    defect = _clash(code=DefectCode.mesh_overlapping_facets, gap=None, stage=STAGE_2D)
    package = rules_author(None, original=MEASURE)(_REGION, (defect,), ())
    assert package is None or package.plan[0]["op"].name != NAME


# THE DECLARATION'S ROUNDING

@pytest.mark.parametrize("value, expected", [
    (0.0, 0.0), (1e-9, 1e-6), (-1e-9, -1e-6), (0.3422131, 0.342214), (-0.3422131, -0.342214),
    (2.5, 2.5)])
def test_a_measured_delta_is_rounded_away_from_zero(value, expected):
    assert _rounded_up(value) == pytest.approx(expected, abs=1e-12)
