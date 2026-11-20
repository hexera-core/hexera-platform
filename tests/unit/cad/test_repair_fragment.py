# Responsibility: Verify fragment_bodies' claim (an edge-through-face mesh failure recorded in the
#                 3D stage), its decline order - above all that INTERPENETRATING bodies are
#                 declined - its exact declarations (solids, faces, measured area, zero volume),
#                 that its flag keeps it out of the catalogue and the rules baseline's MESH_OPS, and
#                 that a package carrying it is judged by WHOLE-SHAPE scans.
# Boundaries: no kernel - the solid lookup, the partner search, the common volume, the general fuse,
#             the splice and the tallies are monkeypatched. The real BOPAlgo_Builder is proved in
#             tests/native/test_native_repair_fragment.py.
from __future__ import annotations

import pytest

import meshpipeline.cad.repair.mesh_probe as mesh_probe
import meshpipeline.cad.repair.operations as operations
import meshpipeline.cad.repair.rules_author as rules_author_module
import meshpipeline.cad.repair.session as session_mod
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
    FragmentBodies,
    OpOutcome,
    catalogue,
)
from meshpipeline.cad.repair.package import PackageBudget, Predicate, TargetPackage
from meshpipeline.cad.repair.regions import Region
from meshpipeline.cad.repair.rules_author import MESH_OPS, mesh_ops, rules_author
from meshpipeline.cad.repair.session import RepairSession

_BOX = {"centroid": [5.0, 5.0, 5.0], "bbox_min": [0.0, 0.0, 0.0], "bbox_max": [10.0, 10.0, 10.0]}
REF = EntityRef(entity_type="face", fingerprint="face:wall", entity_index=4)


def _through(*, stage: str | None = STAGE_3D, entity_type: str = "face",
             code: DefectCode = DefectCode.mesh_edge_through_face) -> EntityDefect:
    measurements = {"mesh_kind": "edge_through_face", "gmsh_surfaces": []}
    if stage is not None:
        measurements["stage"] = stage
    return EntityDefect(code=code, severity=DefectSeverity.error, entity_type=entity_type,
                        entity_index=4, message="a segment and a facet intersect",
                        fingerprint="face:wall", location=dict(_BOX), measurements=measurements)


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FRAGMENT_BODIES", True)


@pytest.fixture
def doubles(monkeypatch, flag_on):
    """Every kernel seam replaced. `configure` sets what each one answers."""
    calls: list[str] = []

    def configure(*, solids=2, owner="A", partner="B", common=0.0, images=(("A2",), ("B2",)),
                  tallies=None, owner_sets=(("A",), ("B",))):
        calls.clear()
        monkeypatch.setattr(operations, "sub_shape",
                            lambda shape, kind, index: "FACE" if index == 4 else None)
        monkeypatch.setattr(operations, "_solid_count", lambda shape: solids)
        owners = () if owner is None else owner if isinstance(owner, tuple) else (owner,)
        monkeypatch.setattr(operations, "_solid_owners",
                            lambda shape, face: calls.append("owner") or owners)
        monkeypatch.setattr(operations, "_meeting_partner",
                            lambda shape, body, face: calls.append("partner") or partner)
        # THE BOUNDED SEAM, not the kernel function: `apply` asks for the common volume through
        # the dispatcher now (row 5), so patching `_common_volume` would be patching something
        # `apply` never calls and every answer here would read as unmeasured.
        monkeypatch.setattr(operations, "_face_owner_sets", lambda shape: list(owner_sets))
        monkeypatch.setattr(operations, "_bounded_common_volume",
                            lambda a, b: calls.append("common") or common)
        # THE BOUNDED SEAM (row 6). `apply` asks for the fuse through the dispatcher's child, so
        # patching the kernel function would patch something it never calls.
        monkeypatch.setattr(operations, "_fuse_in_child",
                            lambda a, b, faces: calls.append("fuse") or (images, "HISTORY"))
        monkeypatch.setattr(operations, "splice",
                            lambda shape, pairs: calls.append(("splice", tuple(pairs)))
                            or ("SPLICED", "CTX"))
        counted = tallies or {"SHAPE": (2, 12, 750.0), "SPLICED": (2, 13, 750.0)}
        monkeypatch.setattr(operations, "_tally", lambda shape: counted[shape])
        monkeypatch.setattr(operations, "_split_fingerprints",
                            lambda before, after, **_: ("face:patch",))
        monkeypatch.setattr(operations, "_diagonal_of", lambda shape: 100.0)
        monkeypatch.setattr(operations, "_face_names",
                            lambda body, **_: tuple(f"face:{body}:{n}" for n in (1, 2)))
        monkeypatch.setattr(operations, "mapping_from_context",
                            lambda *a, **k: operations.EntityMapping.empty())
        return FragmentBodies()

    configure.calls = calls
    return configure


# WHAT IT CLAIMS


def test_answers_only_edge_through_face(flag_on):
    op = FragmentBodies()
    assert op.name == "fragment_bodies"
    assert op.answers == frozenset({DefectCode.mesh_edge_through_face})
    assert op.targets_for(_through()) == (REF,)
    for code in DefectCode:
        if code is not DefectCode.mesh_edge_through_face:
            assert op.targets_for(_through(code=code)) == (), code
    assert op.targets_for(_through(entity_type="shape")) == ()
    assert op.targets_for(object()) == ()


@pytest.mark.parametrize("stage", [STAGE_2D, "", None, mesh_probe.STAGE_IMPORT],
                         ids=["surface", "empty", "unrecorded", "import"])
def test_targets_only_a_volume_stage_failure(flag_on, stage):
    # mesh_probe records a stage on every mesh defect, and an edge through a face is raised while
    # gmsh builds the VOLUME: any other stage, including none, is evidence this op does not read.
    assert FragmentBodies().targets_for(_through(stage=stage)) == ()


def test_targets_nothing_while_the_flag_is_off(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FRAGMENT_BODIES", False)
    assert FragmentBodies().targets_for(_through()) == ()


# WHAT IT DECLINES


def test_interpenetrating_bodies_are_declined(doubles):
    op = doubles(common=12.5)
    outcome = op.apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.shape == "SHAPE"
    reason = outcome.evidence["declined"]
    assert reason.startswith("interpenetrating")
    assert ("the bodies interpenetrate; which body owns the shared volume is a judgement"
            in reason)
    assert outcome.evidence["common_volume_mm3"] == 12.5
    assert AREA_DELTA not in outcome.evidence and VOLUME_DELTA not in outcome.evidence
    assert "fuse" not in doubles.calls


def test_a_common_volume_just_over_the_epsilon_is_interpenetration(doubles):
    outcome = doubles(common=2e-6).apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.evidence["declined"].startswith("interpenetrating")


def test_an_unmeasurable_common_volume_is_never_read_as_touching(doubles):
    outcome = doubles(common=None).apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.shape == "SHAPE"
    assert outcome.evidence["declined"].startswith("unmeasured")
    assert "fuse" not in doubles.calls


@pytest.mark.parametrize("targets, reason", [
    ((), "no face was named"),
    ((REF, EntityRef(entity_type="face", fingerprint="face:other", entity_index=5)),
     "one_face_only"),
    ((EntityRef(entity_type="face", fingerprint="face:gone", entity_index=99),),
     "not_resolved"),
])
def test_declines_what_it_cannot_aim_at(doubles, targets, reason):
    outcome = doubles().apply("SHAPE", targets, tolerance_mm=0.01)
    assert outcome.shape == "SHAPE"
    assert outcome.evidence["declined"].startswith(reason)


def test_a_single_body_part_is_declined(doubles):
    outcome = doubles(solids=1).apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.evidence["declined"].startswith("single_body")
    assert doubles.calls == []


def test_a_face_owned_by_no_solid_is_declined(doubles):
    outcome = doubles(owner=None).apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.evidence["declined"].startswith("no_owner")


def test_a_face_two_bodies_already_share_is_declined(doubles):
    outcome = doubles(owner=("A", "B")).apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.evidence["declined"].startswith("already_shared")
    assert "fuse" not in doubles.calls


def test_no_body_meeting_the_owner_is_declined(doubles):
    outcome = doubles(partner=None).apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.evidence["declined"].startswith("no_partner")
    assert "common" not in doubles.calls


def test_a_body_sharing_a_face_with_a_third_body_is_declined(doubles):
    # B already shares a conformal face with C: rebuilding B as its image would leave C holding
    # the old face.
    op = doubles(owner_sets=(("A",), ("A", "B"), ("B", "C")))
    outcome = op.apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.shape == "SHAPE"
    assert outcome.evidence["declined"].startswith("shared_with_third_body")
    assert "common" not in doubles.calls and "fuse" not in doubles.calls


@pytest.mark.parametrize("owner_sets, shared", [
    ((("A",), ("B",), ("C",)), False),
    ((("A", "B"),), False),              # the pair's own interface is not a third body's
    ((("A", "C"),), True),
    ((("C", "B"),), True),
    ((("C", "D"),), False),              # two OTHER bodies sharing a face is not the pair's
])
def test_third_body_sharing_is_judged_on_the_owner_map(owner_sets, shared):
    assert operations._shares_with_third_body(("A", "B"), owner_sets) is shared


def test_a_fuse_that_changes_neither_body_is_declined(doubles):
    outcome = doubles(images=((), ())).apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.shape == "SHAPE"
    assert outcome.evidence["declined"].startswith("nothing_fragmented")
    assert not any(isinstance(c, tuple) for c in doubles.calls)


def test_a_body_the_fuse_cuts_in_pieces_is_declined(doubles):
    outcome = doubles(images=(("A2", "A3"), ("B2",))).apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.shape == "SHAPE"
    assert outcome.evidence["declined"].startswith("body_split")


def test_a_lost_solid_is_a_decline_never_silent(doubles):
    outcome = doubles(tallies={"SHAPE": (2, 12, 750.0), "SPLICED": (1, 13, 750.0)}).apply(
        "SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.shape == "SHAPE"
    assert outcome.evidence["declined"].startswith("solid_lost")
    assert outcome.evidence["solids_delta"] == -1


def test_declines_while_the_flag_is_off(doubles, monkeypatch):
    op = doubles()
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FRAGMENT_BODIES", False)
    outcome = op.apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.evidence["declined"] == "fragmenting_disabled (CAD_REPAIR_FRAGMENT_BODIES)"
    assert doubles.calls == []


# WHAT IT DECLARES


def test_fragment_declares_its_solids(doubles):
    op = doubles(tallies={"SHAPE": (2, 12, 750.0), "SPLICED": (2, 13, 750.25)})
    outcome = op.apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert "declined" not in outcome.evidence, outcome.evidence
    assert outcome.shape == "SPLICED"
    assert outcome.evidence["solids_delta"] == 0
    assert outcome.evidence["faces_added"] == 1
    # THE MEASURED AREA CHANGE, declared as measured; a fragment moves no volume.
    assert outcome.evidence[AREA_DELTA] == pytest.approx(0.25)
    assert outcome.evidence[VOLUME_DELTA] == 0.0
    assert outcome.evidence["common_volume_mm3"] == 0.0
    assert outcome.added == ("face:patch",)
    # EVERY face of both rebuilt bodies is the op's own geometry, surviving names included.
    assert outcome.rebuilt == ("face:A2:1", "face:A2:2", "face:B2:1", "face:B2:2")
    # BOTH bodies are replaced, each by its own image; nothing else is touched.
    splices = [c for c in doubles.calls if isinstance(c, tuple)]
    assert splices == [("splice", (("A", "A2"), ("B", "B2")))]
    assert [c for c in doubles.calls if not isinstance(c, tuple)] == [
        "owner", "partner", "common", "fuse"]


def test_an_unmodified_body_is_left_in_place(doubles):
    # The fuse may split only one side's face (a small box on a big one): the other body is kept
    # by identity, not replaced by itself.
    doubles(images=(("A2",), ())).apply("SHAPE", (REF,), tolerance_mm=0.01)
    splices = [c for c in doubles.calls if isinstance(c, tuple)]
    assert splices == [("splice", (("A", "A2"),))]
    outcome = doubles(images=(("A2",), ())).apply("SHAPE", (REF,), tolerance_mm=0.01)
    assert outcome.rebuilt == ("face:A2:1", "face:A2:2")   # the untouched body is not rebuilt


# THE FLAG KEEPS IT OUT OF EVERYTHING


def test_absent_from_the_catalogue_and_mesh_ops_when_off():
    assert not rcfg.CAD_REPAIR_FRAGMENT_BODIES
    assert "fragment_bodies" not in {op.name for op in OPERATIONS}
    assert DefectCode.mesh_edge_through_face not in MESH_OPS
    assert [op.name for op in catalogue(fragment_bodies=False)] == [op.name for op in OPERATIONS]
    assert mesh_ops(fragment_bodies=False) == MESH_OPS


def test_present_in_the_catalogue_and_mesh_ops_when_on():
    names = [op.name for op in catalogue(fragment_bodies=True)]
    assert names.count("fragment_bodies") == 1
    assert mesh_ops(fragment_bodies=True)[DefectCode.mesh_edge_through_face] == "fragment_bodies"


def test_catalogue_reads_the_flag_when_not_told(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FRAGMENT_BODIES", True)
    assert "fragment_bodies" in {op.name for op in catalogue()}
    assert DefectCode.mesh_edge_through_face in mesh_ops()
    monkeypatch.setattr(rcfg, "CAD_REPAIR_FRAGMENT_BODIES", False)
    assert "fragment_bodies" not in {op.name for op in catalogue()}
    assert DefectCode.mesh_edge_through_face not in mesh_ops()


# THE RULES BASELINE AIMS IT ONLY AT A VOLUME-STAGE FAILURE

MEASURE = Measure(valid=True, solids=2, shells=2, faces=12, edges=24, vertices=16,
                  diagonal_mm=200.0, max_tolerance_mm=0.01,
                  surface_area_mm2=1000.0, volume_mm3=8000.0)
_REGION = Region(index=0, fingerprints=frozenset({"face:wall"}),
                 bbox_min=(0.0, 0.0, 0.0), bbox_max=(10.0, 10.0, 10.0),
                 face_indices=frozenset({4}))


@pytest.fixture
def baseline_on(monkeypatch, flag_on):
    monkeypatch.setattr(rules_author_module, "MESH_OPS", mesh_ops(fragment_bodies=True))
    monkeypatch.setattr(rules_author_module, "_OPS_BY_NAME",
                        {op.name: op for op in catalogue(fragment_bodies=True)})


def test_rules_author_aims_fragment_at_a_volume_stage_edge_through_face(baseline_on):
    package = rules_author(None, original=MEASURE)(_REGION, (_through(),), ())
    assert package is not None
    item = package.plan[0]
    assert item["op"].name == "fragment_bodies"
    assert [t.fingerprint for t in item["targets"]] == ["face:wall"]
    assert package.predicate.cleared == frozenset({"face:wall"})


@pytest.mark.parametrize("stage", [STAGE_2D, None], ids=["surface_stage", "unrecorded"])
def test_rules_author_refuses_an_edge_through_face_not_from_the_volume_stage(baseline_on, stage):
    author = rules_author(None, original=MEASURE)
    assert author(_REGION, (_through(stage=stage),), ()) is None
    assert {r["reason"] for r in author.refusals} == {"no_operation_claims_it"}


def test_rules_author_with_the_flag_off_still_gives_edge_through_face_no_package():
    author = rules_author(None, original=MEASURE)
    assert author(_REGION, (_through(),), ()) is None
    assert {r["reason"] for r in author.refusals} == {"no_operation_claims_it"}


def test_a_volume_stage_overlap_still_gets_no_package(baseline_on):
    # The whole-shape judgement is bought only by an op that ANSWERS a volume code; the surface
    # mapping for an overlap is unchanged.
    overlap = EntityDefect(code=DefectCode.mesh_overlapping_facets, severity=DefectSeverity.error,
                           entity_type="face", entity_index=4, message="overlap",
                           fingerprint="face:wall", location=dict(_BOX),
                           measurements={"mesh_kind": "overlapping_facets", "stage": STAGE_3D})
    author = rules_author(None, original=MEASURE)
    assert author(_REGION, (overlap,), ()) is None


# THE SESSION JUDGES A 3D-FAMILY PACKAGE BY A WHOLE-SHAPE PROBE

_SESSION_MEASURE = Measure(valid=True, solids=2, shells=2, faces=12, edges=24, vertices=16,
                           diagonal_mm=200.0, max_tolerance_mm=0.01,
                           surface_area_mm2=1000.0, volume_mm3=8000.0)


class _Fragment:
    """A double answering the volume code, as fragment_bodies does."""

    name = "fragment_bodies"
    answers = frozenset({DefectCode.mesh_edge_through_face})
    scope = 8

    def targets_for(self, defect):
        return (EntityRef(entity_type="face", fingerprint=defect.fingerprint,
                          entity_index=defect.entity_index),)

    def apply(self, shape, targets, *, tolerance_mm):
        return OpOutcome(shape="fragmented", evidence={AREA_DELTA: 0.0, VOLUME_DELTA: 0.0})


class _Surface(_Fragment):
    name = "remesh_face"
    answers = frozenset({DefectCode.mesh_overlapping_facets})


def _spies(monkeypatch, probe_results):
    scans: list = []
    probes: list = []

    def scan(shape, **kw):
        scans.append(kw.get("within"))
        return (), True

    def probe(shape, **kw):
        probes.append(kw.get("within"))
        return probe_results[min(len(probes), len(probe_results)) - 1]

    monkeypatch.setattr(session_mod, "_localize_scan", scan)
    monkeypatch.setattr(mesh_probe, "mesh_defects", probe)
    return scans, probes


def _package(op, defect):
    return TargetPackage(
        region=_REGION, intent="clear the edge through the face",
        plan=({"op": op, "targets": op.targets_for(defect), "defect_code": defect.code},),
        predicate=Predicate(cleared=frozenset({"face:wall"}), reclassified=frozenset(),
                            unchanged=frozenset(), max_area_delta_mm2=10.0,
                            max_volume_delta_mm3=10.0),
        budget=PackageBudget(max_ops=1, timeout_s=60.0, model_calls=0, deviation_mm=0.0))


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE_TIMEOUT_S", 120.0)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_HEAL_SELF_INTERSECTIONS", False)


def test_fragment_is_judged_by_a_whole_shape_probe(monkeypatch, live):
    defect = _through()
    # The first (whole-shape) probe sees the failure; every later probe finds the part meshes.
    scans, probes = _spies(monkeypatch, [(defect,), ()])
    session = RepairSession("shape", measurer=lambda s: _SESSION_MEASURE)
    session.defects()
    verdict = session.try_package(_package(_Fragment(), defect))
    # Every scan after the first - the operation's re-inspection and the package's proof - is
    # WHOLE-SHAPE: a scoped (2D-only) probe never builds the volume this failure lives in.
    assert probes[1:] and all(within is None for within in probes[1:]), probes
    assert scans[1:] and all(within is None for within in scans[1:]), scans
    assert verdict.verdicts[0].kept, verdict.verdicts
    assert verdict.status == session_mod.COMMITTED, verdict.reason
    assert session.shape == "fragmented"


def test_a_surface_package_is_still_judged_by_scoped_probes(monkeypatch, live):
    overlap = EntityDefect(code=DefectCode.mesh_overlapping_facets, severity=DefectSeverity.error,
                           entity_type="face", entity_index=4, message="overlap",
                           fingerprint="face:wall", location=dict(_BOX),
                           measurements={"mesh_kind": "overlapping_facets", "stage": STAGE_2D})
    scans, probes = _spies(monkeypatch, [(overlap,), ()])
    session = RepairSession("shape", measurer=lambda s: _SESSION_MEASURE)
    session.defects()
    session.try_package(_package(_Surface(), overlap))
    assert probes[1:] and all(within == _REGION for within in probes[1:]), probes
