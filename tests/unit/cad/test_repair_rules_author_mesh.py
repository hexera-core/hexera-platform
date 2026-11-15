# Responsibility: Prove the deterministic region author aims existing catalogue operations at mesh
#                 failures exactly as MESH_OPS says - open loop to the face fixer, a surface-stage
#                 overlap to the whole-shape fixer - and at nothing else (controller ruling R11b).
# Boundaries: no kernel; the real RulesPlanner over doubles of defects, with no shape to read.
from __future__ import annotations

import pytest

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.mesh_probe import STAGE_2D, STAGE_3D
from meshpipeline.cad.repair.package import PackageBudget, Predicate, TargetPackage
from meshpipeline.cad.repair.regions import Region
from meshpipeline.cad.repair.rules_author import MESH_OPS, rules_author
from meshpipeline.cad.repair.session import RepairSession

MEASURE = Measure(valid=True, solids=1, shells=1, faces=20, edges=60, vertices=40,
                  diagonal_mm=200.0, max_tolerance_mm=0.01,
                  surface_area_mm2=1000.0, volume_mm3=8000.0)
_BOX = {"centroid": [5.0, 5.0, 5.0], "bbox_min": [0.0, 0.0, 0.0], "bbox_max": [10.0, 10.0, 10.0]}
_REGION = Region(index=0, fingerprints=frozenset({"face:f4", "edge:a"}),
                 bbox_min=(0.0, 0.0, 0.0), bbox_max=(10.0, 10.0, 10.0), face_indices=frozenset({4}))


def _mesh(code: DefectCode, *, stage: str = STAGE_2D, fingerprint: str = "face:f4",
          located: bool = True) -> EntityDefect:
    return EntityDefect(code=code, severity=DefectSeverity.error,
                        entity_type="face" if located else "shape",
                        entity_index=4 if located else 0, message=code.value,
                        fingerprint=fingerprint if located else "",
                        location=dict(_BOX) if located else {},
                        measurements={"mesh_kind": code.value, "gmsh_surfaces": [4],
                                      "stage": stage})


def _author():
    return rules_author(None, original=MEASURE)


def test_the_mapping_is_exactly_the_ruling():
    assert MESH_OPS == {DefectCode.mesh_open_loop: "fix_face_boundary",
                        DefectCode.mesh_overlapping_facets: "fix_shape_global"}


@pytest.mark.parametrize("code, op_name", [
    (DefectCode.mesh_open_loop, "fix_face_boundary"),
    (DefectCode.mesh_overlapping_facets, "fix_shape_global"),
])
def test_rules_author_maps_mesh_codes(code, op_name):
    package = _author()(_REGION, (_mesh(code),), ())
    assert package is not None
    assert len(package.plan) == 1
    item = package.plan[0]
    assert item["op"].name == op_name
    assert item["defect_code"] is code
    assert [t.fingerprint for t in item["targets"]] == ["face:f4"]
    assert package.predicate.cleared == frozenset({"face:f4"})
    assert package.predicate.unchanged == frozenset()
    assert package.predicate.reclassified == frozenset()


@pytest.mark.parametrize("defect", [
    _mesh(DefectCode.mesh_edge_through_face, stage=STAGE_3D),
    _mesh(DefectCode.mesh_edge_through_face, stage=""),
    _mesh(DefectCode.mesh_periodic_surface),
    _mesh(DefectCode.mesh_other),
    # A volume-stage overlap: no scoped probe can prove it gone, so no package may claim it.
    _mesh(DefectCode.mesh_overlapping_facets, stage=STAGE_3D),
    _mesh(DefectCode.mesh_no_volume, located=False),
    _mesh(DefectCode.mesh_timeout, located=False),
], ids=["edge_through_face", "edge_through_face_no_stage", "periodic_surface", "other",
        "overlap_stage_3d", "no_volume", "timeout"])
def test_unmapped_mesh_codes_get_no_package(defect):
    author = _author()
    assert author(_REGION, (defect,), ()) is None
    if defect.fingerprint:
        # Reported, not silently skipped: the comparison counts refusals by reason.
        assert {r["defect"] for r in author.refusals} == {defect.fingerprint}


def test_a_mapped_mesh_code_is_not_reported_as_unclaimed():
    author = _author()
    assert author(_REGION, (_mesh(DefectCode.mesh_open_loop),), ()) is not None
    assert author.refusals == []


def test_kernel_defects_come_first_and_mesh_failures_after():
    # Flag-off behaviour is unchanged by construction: the rules see exactly the kernel defects
    # they always saw, and a mesh failure is aimed at only when the rules have nothing.
    wire_gap = EntityDefect(code=DefectCode.wire_gap, severity=DefectSeverity.error,
                            entity_type="face", entity_index=4, message="gap",
                            fingerprint="face:f4", location=dict(_BOX))
    package = _author()(_REGION, (wire_gap, _mesh(DefectCode.mesh_open_loop)), ())
    assert package is not None and package.plan[0]["defect_code"] is DefectCode.wire_gap


def test_a_mesh_operation_that_was_tried_is_not_proposed_again():
    from meshpipeline.cad.repair.session import OpVerdict, PackageVerdict

    tried = PackageVerdict(region_index=0, status="reverted", verdicts=(OpVerdict(
        op="fix_face_boundary", targets=("face:f4",), defect_code="mesh_open_loop",
        status="rolled_back", reason="target_remains"),))
    assert _author()(_REGION, (_mesh(DefectCode.mesh_open_loop),), (tried,)) is None


def test_timeout_region_cannot_claim_meshes(monkeypatch):
    timeout = _mesh(DefectCode.mesh_timeout, located=False)
    # The author does not claim it cleared: it has no fingerprint, so nothing could prove it gone.
    author = _author()
    assert author(_REGION, (timeout,), ()) is None

    # And a package that claims it anyway is never executed, let alone committed: the empty
    # fingerprint names no entity, so there is nothing for evaluate to look for in the scan.
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MESH_PROBE", "live")
    session = RepairSession("shape", measurer=lambda s: MEASURE,
                            scanner=lambda s: ((timeout,), True))
    package = TargetPackage(
        region=_REGION, intent="claim the part meshes",
        plan=({"op": object(), "targets": (), "defect_code": DefectCode.mesh_timeout},),
        predicate=Predicate(cleared=frozenset({""}), reclassified=frozenset(),
                            unchanged=frozenset(), max_area_delta_mm2=1.0,
                            max_volume_delta_mm3=1.0),
        budget=PackageBudget(max_ops=1, timeout_s=60.0, model_calls=0, deviation_mm=0.0))
    verdict = session.try_package(package)
    assert verdict.status != "committed"
    assert session.shape == "shape"


# --- P2-R8: severity first at the candidate choice ------------------------------------------------

def _kernel(code, severity, fingerprint, index):
    return EntityDefect(code=code, severity=severity, entity_type="edge", entity_index=index,
                        message=code.value, fingerprint=fingerprint, location=dict(_BOX))


_REGION_R8 = Region(index=0, fingerprints=frozenset({"face:f4", "edge:a", "edge:b", "edge:c"}),
                    bbox_min=(0.0, 0.0, 0.0), bbox_max=(10.0, 10.0, 10.0),
                    face_indices=frozenset({4}))


def _picks(defects):
    package = _author()(_REGION_R8, tuple(defects), ())
    return None if package is None else (package.plan[0]["op"].name,
                                         package.plan[0]["targets"][0].fingerprint)


def test_kernel_only_proposal_order_is_unchanged():
    # Pinned from today's behaviour (no mesh defect present, so the severity branch never runs):
    # the planner's own severity/entity ordering decides, however the list arrives.
    warn_b = _kernel(DefectCode.small_edge, DefectSeverity.warning, "edge:b", 2)
    warn_a = _kernel(DefectCode.small_edge, DefectSeverity.warning, "edge:a", 1)
    expected = ("fix_small_edges", "edge:a")
    assert _picks([warn_b, warn_a]) == expected
    assert _picks([warn_a, warn_b]) == expected


def test_a_mesh_error_is_proposed_before_a_kernel_warning():
    warn = _kernel(DefectCode.small_edge, DefectSeverity.warning, "edge:a", 1)
    assert _picks([warn, _mesh(DefectCode.mesh_open_loop)]) == ("fix_face_boundary", "face:f4")


def test_a_kernel_error_still_comes_before_a_mesh_error():
    err = _kernel(DefectCode.wire_gap, DefectSeverity.error, "edge:a", 1)
    assert _picks([err, _mesh(DefectCode.mesh_open_loop)])[1] == "edge:a"


def test_an_unmapped_mesh_error_does_not_displace_a_kernel_warning():
    warn = _kernel(DefectCode.small_edge, DefectSeverity.warning, "edge:a", 1)
    unmapped = _mesh(DefectCode.mesh_edge_through_face, stage=STAGE_3D)
    assert _picks([warn, unmapped]) == ("fix_small_edges", "edge:a")


def test_siblings_of_one_contact_pair_are_ineffective_together():
    from meshpipeline.cad.repair.session import OpVerdict, PackageVerdict

    def sibling(fp):
        d = _mesh(DefectCode.mesh_open_loop, fingerprint=fp)
        d.measurements["pair"] = "solids:1+2"
        return d

    region = Region(index=0, fingerprints=frozenset({"face:f4", "face:f5"}),
                    bbox_min=(0.0, 0.0, 0.0), bbox_max=(10.0, 10.0, 10.0),
                    face_indices=frozenset({4}))
    tried = PackageVerdict(region_index=0, status="reverted", verdicts=(OpVerdict(
        op="fix_face_boundary", targets=("face:f4",), defect_code="mesh_open_loop",
        status="rolled_back", reason="target_remains"),))
    assert _author()(region, (sibling("face:f4"), sibling("face:f5")), (tried,)) is None
