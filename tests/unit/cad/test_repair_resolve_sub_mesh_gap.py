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


def test_a_decline_spends_the_gap_operation_at_once(baseline_on):
    package = rules_author(None, original=MEASURE)(
        _REGION, (_clash(),), (_tried(NAME, declined=True),))
    assert package is not None and package.plan[0]["op"].name == "fragment_bodies"


def test_a_failed_split_is_retried_finer_down_the_ladder(baseline_on):
    from meshpipeline.cad.repair.gap_split import SAG_LADDER

    author = rules_author(None, original=MEASURE)
    tried: tuple = ()
    for rung, share in enumerate(SAG_LADDER):
        package = author(_REGION, (_clash(),), tried)
        assert package is not None and package.plan[0]["op"].name == NAME, rung
        assert package.plan[0]["op"].sag_share == share
        tried = (*tried, _tried(NAME))
    # THE LADDER SPENT, the code gets its own choice.
    package = author(_REGION, (_clash(),), tried)
    assert package is not None and package.plan[0]["op"].name == "fragment_bodies"


def _gap_clash(**kwargs) -> EntityDefect:
    defect = _clash(**kwargs)
    defect.measurements["clash_kind"] = "gap"
    return defect


def _warning() -> EntityDefect:
    return EntityDefect(code=DefectCode.small_face, severity=DefectSeverity.warning,
                        entity_type="face", entity_index=2, message="sliver",
                        fingerprint="face:sliver", location=dict(_BOX), measurements={})


_REGION2 = Region(index=0, fingerprints=frozenset({"face:wall", "face:sliver"}),
                  bbox_min=(0.0, 0.0, 0.0), bbox_max=(10.0, 10.0, 10.0),
                  face_indices=frozenset({1, 2}))


def test_a_measured_gap_goes_first_then_the_kernel_cure_gets_its_turn(baseline_on, monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FIX_SMALL_FACES", True)
    author = rules_author(None, original=MEASURE)
    first = author(_REGION2, (_gap_clash(), _warning()), ())
    assert first is not None and first.plan[0]["op"].name == NAME
    # THE GAP ATTEMPT FAILED: the kernel's cure is next (0060_00608290's fix_small_faces).
    second = author(_REGION2, (_gap_clash(), _warning()), (_tried(NAME),))
    assert second is not None and second.plan[0]["op"].name != NAME


def test_gap_tries_counts_failures_declines_and_the_last_outcome():
    from meshpipeline.cad.repair.rules_author import gap_tries

    tries = gap_tries((_tried(NAME), _tried(NAME), _tried(NAME, declined=True)))
    assert tries.rung("face:wall") == 2 and "face:wall" in tries.declined
    assert tries.last_failed and tries.spent("face:wall")
    assert not gap_tries(()).last_failed


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


# THE GAP PRE-PASS (region_loop._gap_prepass): before any author, every gap clash is offered the
# operation through the ordinary try_package, down its ladder, inside its share of the clock.

class _Work:
    """Just enough of a RepairSession: a defect list, a log, the upload's measure, try_package."""

    def __init__(self, defects, *, commit_on: int | None = None):
        self._defects = list(defects)
        self.log: list = []
        self.original = MEASURE
        self.offered: list = []
        self.commit_on = commit_on

    def defects(self):
        return tuple(self._defects)

    def try_package(self, package):
        op = package.plan[0]["op"]
        self.offered.append(op.sag_share)
        commit = self.commit_on is not None and len(self.offered) == self.commit_on
        self.log.append(op.name)
        verdict = PackageVerdict(region_index=package.region.index,
                                 status="committed" if commit else "reverted",
                                 verdicts=(OpVerdict(op=op.name, targets=("face:wall",),
                                                     defect_code="mesh_edge_through_face",
                                                     status="kept" if commit else "rolled_back",
                                                     reason="" if commit else "target_remains",
                                                     evidence={}),))
        if commit:
            self._defects = []
        return verdict


def _prepass(work, *, left: float = 1000.0, max_ops: int = 6):
    from meshpipeline.cad.repair import region_loop

    return region_loop._gap_prepass(work, (_REGION,), remaining=lambda: left, reserve=10.0,
                                    tolerance_mm=1e-4, max_ops=max_ops)


def test_the_prepass_walks_the_ladder_until_a_split_holds(baseline_on):
    work = _Work((_gap_clash(),), commit_on=2)
    out = _prepass(work)
    assert [v.status for _, v in out] == ["reverted", "committed"]
    assert work.offered == [0.25, 0.125]


def test_the_prepass_stops_when_the_ladder_is_spent(baseline_on):
    from meshpipeline.cad.repair.gap_split import SAG_LADDER

    work = _Work((_gap_clash(),))
    out = _prepass(work)
    assert len(out) == len(SAG_LADDER) and work.offered == list(SAG_LADDER)


def test_the_prepass_ignores_what_is_not_a_measured_gap(baseline_on):
    work = _Work((_clash(), _clash(gap=0.0), _warning()))
    assert _prepass(work) == [] and work.offered == []


def test_the_prepass_respects_its_clock_share_and_the_flag(baseline_on, monkeypatch):
    work = _Work((_gap_clash(),))
    assert _prepass(work, left=5.0) == []          # inside the final reserve
    monkeypatch.setattr(rcfg, "CAD_REPAIR_RESOLVE_SUB_MESH_GAP", False)
    assert _prepass(_Work((_gap_clash(),))) == []


def test_every_faces_first_rung_comes_before_any_faces_second(baseline_on):
    """0051_00519054: three rungs on one doomed face spent what a neighbour's coarse split needed."""
    import dataclasses

    other = dataclasses.replace(_gap_clash(), entity_index=2, fingerprint="face:other")
    region = Region(index=0, fingerprints=frozenset({"face:wall", "face:other"}),
                    bbox_min=(0.0, 0.0, 0.0), bbox_max=(10.0, 10.0, 10.0),
                    face_indices=frozenset({1, 2}))

    class _Two(_Work):
        def try_package(self, package):
            self.aimed = [*getattr(self, "aimed", []), package.plan[0]["targets"][0].fingerprint]
            verdict = super().try_package(package)
            fp = package.plan[0]["targets"][0].fingerprint
            return dataclasses.replace(verdict, verdicts=tuple(
                dataclasses.replace(v, targets=(fp,)) for v in verdict.verdicts))

    from meshpipeline.cad.repair import region_loop

    work = _Two((_gap_clash(), other))
    region_loop._gap_prepass(work, (region,), remaining=lambda: 1000.0, reserve=10.0,
                             tolerance_mm=1e-4, max_ops=6)
    assert work.aimed[:2] == ["face:wall", "face:other"]
    assert work.offered[:2] == [0.25, 0.25] and work.offered[2] == 0.125


def test_the_export_check_writes_in_the_delivery_unit(monkeypatch):
    """0060_00600919 is a METRE upload: its split read back valid from a millimetre STEP and
    invalid from the metre STEP the customer gets. The check writes what delivery writes."""
    import meshpipeline.cad.repair.conservative as conservative
    import meshpipeline.cad.repair.gap_split as gs
    import meshpipeline.cad.repair.operations as operations

    seen: list = []
    monkeypatch.setattr(operations, "_is_valid", lambda shape: True)
    monkeypatch.setattr(conservative, "_write_step",
                        lambda shape, path, *, unit=None: seen.append(unit))
    monkeypatch.setattr(gs, "_run_child", lambda args, budget: ({"valid": False}, ""))
    assert gs._export_check(object(), object(), budget_s=1.0) .startswith("export_invalidates")
    with gs.delivering_in("m"):
        gs._export_check(object(), object(), budget_s=1.0)
    assert seen == ["mm", "m"]
    monkeypatch.setattr(gs, "_run_child", lambda args, budget: (None, "kernel_timeout (x)"))
    assert gs._export_check(object(), object(), budget_s=1.0).startswith("kernel_timeout")
