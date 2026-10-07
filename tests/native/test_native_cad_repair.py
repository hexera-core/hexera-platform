# Responsibility: Verify conservative repair against the real OpenCASCADE kernel - a sound part survives it untouched in substance, and an unsafe result is refused.
# Boundaries: the native tier, because this is the only place the CAD kernel actually exists. The
#             caps' arithmetic is unit-tested; what is proved here is that the kernel's real
#             output passes through the same judgement.
from __future__ import annotations

import pytest
from tests.cad_fixtures import write_step

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair.conservative import (
    RepairRefused,
    RepairUnavailable,
    repair_step_file,
)
from meshpipeline.cad.repair.contracts import RepairProfile

pytest.importorskip("OCP.ShapeFix", reason="the CAD kernel is a native-tier dependency")


@pytest.fixture(autouse=True)
def _opted_in(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_DEVIATION_RATIO", 0.001)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_TOLERANCE_MM", 0.1)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ALLOW_FACE_REMOVAL", False)


def test_a_sound_part_passes_through_repair_without_losing_geometry(tmp_path):
    source = write_step(tmp_path / "box.step", "MM")
    out = tmp_path / "box-repaired.step"

    report = repair_step_file(source, out)

    assert out.exists() and out.stat().st_size > 0
    measured = {m.name: m.value for m in report.measurements}
    before, after = measured["before"], measured["after"]
    # A VALID PART MUST COME OUT VALID AND WHOLE. A conservative pass over sound geometry has
    # nothing to fix, and anything it changed about the face count or the size would be damage.
    assert after["faces"] == before["faces"]
    assert after["solids"] == before["solids"]
    assert after["valid"] is True
    assert abs(after["diagonal_mm"] - before["diagonal_mm"]) / before["diagonal_mm"] < 1e-9
    # and it says what it did, in operations a reviewer can read
    assert [op["name"] for op in report.operations]
    assert all(op["mutated"] for op in report.operations)


def test_the_customers_file_is_not_modified(tmp_path):
    source = write_step(tmp_path / "box.step", "MM")
    original = source.read_bytes()

    repair_step_file(source, tmp_path / "out.step")

    # THE ONE INVARIANT THIS WHOLE SERVICE RESTS ON: the bytes the customer sent are still theirs.
    assert source.read_bytes() == original


def test_a_tolerance_cap_of_zero_refuses_the_kernels_own_output(tmp_path, monkeypatch):
    # Not a contrived number: it is how a cap behaves when the kernel leaves ANY tolerance at all,
    # which is the case this refusal exists for. The repaired shape is produced and then declined.
    source = write_step(tmp_path / "box.step", "MM")
    out = tmp_path / "out.step"
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_TOLERANCE_MM", 0.0)

    with pytest.raises(RepairRefused) as refused:
        repair_step_file(source, out)

    assert "cap" in str(refused.value)
    assert refused.value.measurements["before"]["faces"] > 0
    # REFUSED MEANS NOTHING WAS DELIVERED: no half-written artifact for a later step to pick up
    assert not out.exists()


def test_the_switch_is_checked_before_the_kernel_is_touched(tmp_path, monkeypatch):
    source = write_step(tmp_path / "box.step", "MM")
    monkeypatch.setattr(rcfg, "CAD_REPAIR_ENABLED", False)

    with pytest.raises(RepairUnavailable, match="CAD_REPAIR_ENABLED"):
        repair_step_file(source, tmp_path / "out.step")

    assert not (tmp_path / "out.step").exists()


def test_only_the_conservative_profile_runs_here(tmp_path):
    source = write_step(tmp_path / "box.step", "MM")
    with pytest.raises(RepairUnavailable, match="operator"):
        repair_step_file(source, tmp_path / "out.step", profile=RepairProfile.mesh_ready)


def test_a_real_repaired_part_can_be_promoted_to_the_runs_geometry(tmp_path):
    """The promotion path against the real kernel: repair a part, then prove the result stages.

    WHAT THIS DOES NOT PROVE. The roadmap's acceptance for this stage is "the original fails
    staging, the repaired output passes". The second half is what runs here; the first needs a
    fixture whose geometry OpenCASCADE genuinely refuses to stage, and a part that broken cannot
    be authored reliably from the kernel's own constructors - every shape they build is sound by
    construction. Until such a fixture exists (a real customer file, licensed and reduced), the
    honest claim is this one: a repaired part is accepted only because it provably staged.
    """
    from meshpipeline.contracts.geometry_source import (
        GeometryInterpretationRef,
        GeometrySourceRef,
        MaterializedGeometry,
        sha256_of,
    )
    from meshpipeline.pipeline.repair_promote import derived_interpretation, promote

    source = write_step(tmp_path / "part.step", "MM")
    # THE HANDLE IS BUILT FROM THE REAL FILE, not from tests._geometry_support: that helper WRITES
    # placeholder bytes at the path it is given, which would overwrite the STEP this test just
    # authored and leave the kernel parsing a comment. The handle has to describe the bytes that
    # are actually there - that is the whole contract being exercised.
    src_digest, src_size = sha256_of(source)
    original = MaterializedGeometry(
        ref=GeometrySourceRef(
            source_id="native-original-source", owner_id="native",
            object_key="sources/native-original-source", sha256=src_digest,
            size_bytes=src_size, original_filename="part.step", suffix_hint=".step"),
        interpretation=GeometryInterpretationRef(
            interpretation_id="native-fixture", geometry_source_id="native-original-source",
            unit="mm", scale_to_metres=0.001, basis="user_confirmed"),
        local_path=source)
    state = {"job_id": "native-1", "engine": "gmsh", "geometry": original.to_state()}

    repaired_path = tmp_path / "part-repaired.step"
    repair_step_file(source, repaired_path)
    assert repaired_path.exists()

    digest, size = sha256_of(repaired_path)
    # THE REPAIRED BYTES ARE THEIR OWN SOURCE: the digest is the identity, so a handle claiming
    # the original's digest for different bytes would make every later provenance claim a lie.
    repaired_id = "native-repaired-source"
    repaired = MaterializedGeometry(
        ref=GeometrySourceRef(
            source_id=repaired_id, owner_id=original.ref.owner_id,
            object_key=f"sources/{repaired_id}", sha256=digest, size_bytes=size,
            original_filename="part-repaired.step", suffix_hint=".step"),
        interpretation=derived_interpretation(original, source_id=repaired_id),
        local_path=repaired_path)

    update = promote(state, repaired=repaired, engine="gmsh", attempt=1)

    # it staged for the real engine through the real kernel, so it is what the run meshes now
    assert update["geometry"]["ref"]["sha256"] == digest
    lineage = update["repair_lineage"]
    assert lineage["original"]["sha256"] == original.ref.sha256
    assert lineage["engine_staged_for"] == "gmsh"
    # the unit was carried, not re-resolved
    assert update["geometry"]["interpretation"]["unit"] == \
        state["geometry"]["interpretation"]["unit"]


# LOCALISED DEFECTS, AND THE REPAIR THAT CLOSES THEM


def test_a_sound_part_has_no_located_defects(tmp_path):
    from meshpipeline.cad.repair.brep import inspect_brep_file

    report = inspect_brep_file(write_step(tmp_path / "box.step", "MM"))

    assert report.defects == () and report.entities == ()
    assert report.summary == "No repair needed."


def test_a_missing_face_is_located_as_an_edge_loop_not_a_verdict(tmp_path):
    from tests.cad_fixtures import write_open_shell_step

    from meshpipeline.cad.repair.brep import inspect_brep_file

    report = inspect_brep_file(write_open_shell_step(tmp_path / "holed.step"))

    measured = {m.name: m.value for m in report.measurements}
    # VALID, AND STILL UNMESHABLE - which is the point. The shape is invalid as an open SOLID in
    # memory, but the STEP round trip brings it back as an open SHELL, and an open shell is
    # perfectly valid B-rep. `BRepCheck_Analyzer` is therefore satisfied while the part still has
    # a hole in it, so validity alone can never be the service's test of whether a file will mesh.
    assert measured["is_valid"] is True
    # The opening is attributed to the edges that bound it, which is what validity missed.
    assert [d.code.value for d in report.defects] == ["open_shell"]
    assert len(report.entities) == 1
    entity = report.entities[0]
    assert entity["code"] == "open_shell"
    assert entity["entity_type"] == "edge"
    # the four edges that bounded the face that is gone, and where the hole is
    assert len(entity["measurements"]["boundary_edges"]) == 4
    assert entity["location"]["centroid"]
    assert "located" in measured and measured["located"]["by_code"] == {"open_shell": 1}


def test_triage_recommends_repairing_a_hole_it_can_point_at(tmp_path):
    from tests.cad_fixtures import write_open_shell_step

    from meshpipeline.cad.repair.brep import inspect_brep_file
    from meshpipeline.cad.repair.triage import ROUTE_CONSERVATIVE_REPAIR, recommend

    report = inspect_brep_file(write_open_shell_step(tmp_path / "holed.step"))
    advice = recommend(repair_status="repairable", report=report.to_dict(),
                       target_engine="gmsh")

    # THE BEHAVIOUR THAT WAS IMPOSSIBLE BEFORE LOCALISATION: an invalid STEP routed to automatic
    # repair rather than back to the customer, because we can say what is wrong with it.
    assert advice.route == ROUTE_CONSERVATIVE_REPAIR
    assert advice.targets and advice.targets[0]["code"] == "open_shell"
    assert advice.targets[0]["measurements"]["boundary_edges"]


def test_sewing_alone_cannot_close_a_hole_and_the_repair_says_which_it_left(tmp_path):
    """The limit of the conservative profile, stated by the code rather than assumed.

    Sewing stitches coincident boundaries. A face that is simply missing has nothing to stitch to,
    so the hole survives every operation in the profile - and the report has to say so, because a
    repair that silently leaves the defect it was run for is worse than one that declines.
    """
    from tests.cad_fixtures import write_open_shell_step

    from meshpipeline.cad.repair.brep import inspect_brep_file

    source = write_open_shell_step(tmp_path / "holed.step")
    before = inspect_brep_file(source)
    assert [d.code.value for d in before.defects] == ["open_shell"]

    out = tmp_path / "holed-repaired.step"
    report = repair_step_file(source, out)

    measured = {m.name: m.value for m in report.measurements}
    # filling is off by default, so nothing was patched and the loop is reported as left
    assert measured["holes_filled"] == []
    assert measured["holes_left"], "a hole left unfilled must be reported, not dropped"
    # and the re-inspection still finds it - which is the honest outcome
    after = inspect_brep_file(out)
    assert [d.code.value for d in after.defects] == ["open_shell"]


def test_with_filling_enabled_a_bounded_hole_is_closed_and_the_patch_is_declared(tmp_path,
                                                                                 monkeypatch):
    """Filling ON closes an opening within the cap, and says how much surface it added.

    The fixture's opening is one whole face of a cube, which the span cap refuses as a missing
    wall rather than a hole - so the cap is raised here deliberately to exercise the patching
    itself. That the default cap declines this very shape is the point of the cap.
    """
    from tests.cad_fixtures import write_open_shell_step

    from meshpipeline.cad.repair.brep import inspect_brep_file

    monkeypatch.setattr(rcfg, "CAD_REPAIR_FILL_PLANAR_HOLES", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_HOLE_SPAN_RATIO", 1.0)

    source = write_open_shell_step(tmp_path / "holed.step")
    out = tmp_path / "holed-repaired.step"
    report = repair_step_file(source, out)

    measured = {m.name: m.value for m in report.measurements}
    # A REVIEWER APPROVING THIS IS APPROVING AN INVENTION, so the patch is declared with its size
    assert len(measured["holes_filled"]) == 1
    assert measured["holes_filled"][0]["span_ratio"] > 0
    assert "fill_planar_holes" in [op["name"] for op in report.operations]

    after = inspect_brep_file(out)
    # the located defect is gone, proved by re-inspecting the written file rather than by the
    # repair asserting its own success
    assert after.entities == (), f"still located: {after.entities}"
    after_measured = {m.name: m.value for m in after.measurements}
    assert after_measured["is_valid"] is True
    assert after_measured["solids"] >= 1


def test_the_span_cap_refuses_to_lid_a_missing_wall(tmp_path, monkeypatch):
    from tests.cad_fixtures import write_open_shell_step

    monkeypatch.setattr(rcfg, "CAD_REPAIR_FILL_PLANAR_HOLES", True)
    # the default cap: one face of a cube spans more of it than this allows
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_HOLE_SPAN_RATIO", 0.75)

    report = repair_step_file(write_open_shell_step(tmp_path / "holed.step"),
                              tmp_path / "out.step")

    measured = {m.name: m.value for m in report.measurements}
    assert measured["holes_filled"] == []
    assert measured["holes_left"][0]["reason"] == "too_large"


def test_a_missing_bore_wall_is_never_sealed_shut(tmp_path, monkeypatch):
    """The part-ruining case, refused on the real geometry.

    Both rims are planar, congruent and far inside the size cap, so every other guard in this
    module would let them through. Patching them would hand the customer a plate with no bolt
    hole - so the discriminator refuses, and says which loops and why.
    """
    from tests.cad_fixtures import write_missing_bore_wall_step

    monkeypatch.setattr(rcfg, "CAD_REPAIR_FILL_PLANAR_HOLES", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_HOLE_SPAN_RATIO", 1.0)

    source = write_missing_bore_wall_step(tmp_path / "bore.step")
    report = repair_step_file(source, tmp_path / "bore-repaired.step")

    measured = {m.name: m.value for m in report.measurements}
    assert measured["holes_filled"] == [], "a through-hole was sealed"
    reasons = [h["reason"] for h in measured["holes_left"]]
    assert reasons.count("paired_rims") == 2, measured["holes_left"]
    assert "seal a through-hole" in measured["holes_left"][0]["detail"]


def test_the_bore_rims_are_located_as_two_separate_openings(tmp_path):
    from tests.cad_fixtures import write_missing_bore_wall_step

    from meshpipeline.cad.repair.brep import inspect_brep_file

    report = inspect_brep_file(write_missing_bore_wall_step(tmp_path / "bore.step"))

    # the inspection's job is to find them; deciding what to do about them is not its call
    openings = [e for e in report.entities if e["code"] == "open_shell"]
    assert len(openings) == 2
    spans = {round(o["measurements"]["boundary_length"], 3) for o in openings}
    assert len(spans) == 1, f"the two rims of one bore should measure the same: {spans}"
