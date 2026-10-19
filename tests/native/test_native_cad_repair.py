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


def test_a_sound_part_is_left_alone_and_nothing_is_written_for_it(tmp_path):
    """A sound part has nothing to repair, so it gets no delivery - not a re-export.

    It used to be written out and reported as repaired. Nothing was kept, so those bytes were a
    STEP round trip of the customer's own file, which changes topology for no repair at all; the
    run is better off on the upload it already has.
    """
    source = write_step(tmp_path / "box.step", "MM")
    out = tmp_path / "box-repaired.step"

    with pytest.raises(RepairRefused) as refused:
        repair_step_file(source, out)

    measured = refused.value.measurements
    before, after = measured["before"], measured["after"]
    # A VALID PART MUST COME OUT VALID AND WHOLE. A conservative pass over sound geometry has
    # nothing to fix, and anything it changed about the face count or the size would be damage.
    assert after["faces"] == before["faces"]
    assert after["solids"] == before["solids"]
    assert after["valid"] is True
    assert abs(after["diagonal_mm"] - before["diagonal_mm"]) / before["diagonal_mm"] < 1e-9
    # and it says what it did: a sound part had nothing to repair, so no operation ran on it
    assert measured["operations_attempted"] == 0 and measured["operations_kept"] == 0
    assert not out.exists()


def test_the_customers_file_is_not_modified(tmp_path):
    source = write_step(tmp_path / "box.step", "MM")
    original = source.read_bytes()

    # a sound part is now declined for having nothing to deliver; the invariant below is about
    # the SOURCE either way, and holds on every path through this function.
    with pytest.raises(RepairRefused):
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
    from tests.cad_fixtures import write_loose_faces_step

    from meshpipeline.contracts.geometry_source import (
        GeometryInterpretationRef,
        GeometrySourceRef,
        MaterializedGeometry,
        sha256_of,
    )
    from meshpipeline.pipeline.repair_promote import derived_interpretation, promote

    # THE LOOSE-FACES FIXTURE, because promotion is only reachable for a run that KEPT something:
    # a run that repaired nothing is declined before anything is written, so a sound box can no
    # longer carry this test.
    source = write_loose_faces_step(tmp_path / "part.step")
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
    with pytest.raises(RepairRefused) as refused:
        repair_step_file(source, out)

    measured = refused.value.measurements
    # filling is off by default, so nothing was patched, and the loop says it did not succeed
    assert measured["resolved"] is False
    assert measured["operations_kept"] == 0
    # NOTHING KEPT MEANS NOTHING DELIVERED. The hole is still reported - by the inspection of the
    # file the customer sent, which is what the run goes on using.
    assert not out.exists()
    assert [d.code.value for d in inspect_brep_file(source).defects] == ["open_shell"]


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
    kept = [op for op in report.operations if op["name"] == "fill_planar_hole"]
    assert len(kept) == 1 and kept[0]["status"] == "kept" and kept[0]["mutated"]
    assert kept[0]["delta"], "the patch was kept without saying how much it added"
    assert measured["resolved"] is True

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

    out = tmp_path / "out.step"
    with pytest.raises(RepairRefused) as refused:
        repair_step_file(write_open_shell_step(tmp_path / "holed.step"), out)

    measured = refused.value.measurements
    # the patch is attempted-and-undone or never made; either way nothing was kept, so the run
    # stays on the customer's own file with the wall still open
    assert measured["operations_kept"] == 0
    assert measured["resolved"] is False
    assert not out.exists()


@pytest.mark.parametrize("profile", ["round", "hex"])
def test_a_missing_bore_wall_is_never_sealed_shut(tmp_path, monkeypatch, profile):
    """The part-ruining case, refused on the real geometry - for a round bore AND a hex hole.

    Both rims are planar, congruent and far inside the size cap, so every other guard in this
    module would let them through. Patching them would hand the customer a plate with no bolt
    hole - so the discriminator refuses, and says which loops and why. The hex case is the
    reviewer's reproduction: a roundness test let its fill be kept and the hole was sealed.
    """
    from tests.cad_fixtures import write_missing_bore_wall_step

    monkeypatch.setattr(rcfg, "CAD_REPAIR_FILL_PLANAR_HOLES", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_HOLE_SPAN_RATIO", 1.0)

    source = write_missing_bore_wall_step(tmp_path / "bore.step", profile=profile)
    out = tmp_path / "bore-repaired.step"
    # nothing can be kept once both rims are refused, so the run is declined rather than delivered
    with pytest.raises(RepairRefused) as refused:
        repair_step_file(source, out)

    measured = refused.value.measurements
    assert measured["operations_kept"] == 0, "a through-hole was sealed"
    refusals = measured["refusals"]
    assert [r["reason"] for r in refusals].count("paired_rim") == 2, refusals
    assert "seal a through-hole" in refusals[0]["detail"]
    assert not out.exists()


@pytest.mark.parametrize("kind", [
    "angled10", "angled30", "angled45", "wedge3", "wedge10",
    "round", "hex", "slot", "square", "pent", "tri"])
def test_a_through_passage_of_any_shape_angle_or_draft_is_never_sealed(tmp_path, monkeypatch,
                                                                       kind):
    """END TO END, AT THE DEFAULT CAPS. Every angled and wedge case here was delivered "Repaired
    within the conservative caps" - faces 6 -> 8, volume back to the full plate - while the guard
    demanded parallel rims on one normal. Both rims must be refused as a pair and nothing kept."""
    from tests.cad_fixtures import write_missing_passage_wall_step

    monkeypatch.setattr(rcfg, "CAD_REPAIR_FILL_PLANAR_HOLES", True)

    source = write_missing_passage_wall_step(tmp_path / f"{kind}.step", kind)
    out = tmp_path / f"{kind}-repaired.step"
    with pytest.raises(RepairRefused) as refused:
        repair_step_file(source, out)

    measured = refused.value.measurements
    assert measured["operations_kept"] == 0, f"the {kind} through-hole was sealed"
    assert [r["reason"] for r in measured["refusals"]].count("paired_rim") == 2, measured["refusals"]
    assert not out.exists()


@pytest.mark.parametrize("reverse", [False, True], ids=["file-order", "reversed-order"])
@pytest.mark.parametrize("kind", ["dbar0", "dbar10", "flipbar0", "flipbar10"])
def test_a_through_hole_with_one_curved_rim_is_never_sealed_in_either_face_order(
        tmp_path, monkeypatch, kind, reverse):
    """ONE RIM A SADDLE, THE OTHER FLAT. Skipping the saddle left the flat rim with nothing to pair
    with: on the reversed face order its fill was KEPT (a 50 mm2 disc sealing the hole) and on the
    file order only the stall limit stopped the run first. The saddle is now judged on its best-fit
    axis and the two rims PAIR - a real answer, not the "could not run" fail-closed stopgap - in
    whatever order the faces arrive. (dbar10/flipbar10 tilt the saddle 19.6 deg: past the coaxial
    rule, paired by the passage rule.)"""
    from tests.cad_fixtures import write_missing_passage_wall_step

    monkeypatch.setattr(rcfg, "CAD_REPAIR_FILL_PLANAR_HOLES", True)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_STALL_LIMIT", 6)   # not saved by stopping early

    source = write_missing_passage_wall_step(tmp_path / f"{kind}.step", kind, reverse=reverse)
    out = tmp_path / f"{kind}-repaired.step"
    with pytest.raises(RepairRefused) as refused:
        repair_step_file(source, out)

    measured = refused.value.measurements
    assert measured["operations_kept"] == 0, f"the {kind} through-hole was sealed"
    assert not any(o.get("name") == "fill_planar_hole" for o in measured["operations"]), (
        "a fill was attempted on one rim of a through-hole")
    rims = [r for r in measured["refusals"] if r["reason"] == "paired_rim"]
    assert len(rims) == 2 and not any("could not run" in r["detail"] for r in rims), rims
    assert not out.exists()


def test_the_bore_rims_are_located_as_two_separate_openings(tmp_path):
    from tests.cad_fixtures import write_missing_bore_wall_step

    from meshpipeline.cad.repair.brep import inspect_brep_file

    report = inspect_brep_file(write_missing_bore_wall_step(tmp_path / "bore.step"))

    # the inspection's job is to find them; deciding what to do about them is not its call
    openings = [e for e in report.entities if e["code"] == "open_shell"]
    assert len(openings) == 2
    spans = {round(o["measurements"]["boundary_length"], 3) for o in openings}
    assert len(spans) == 1, f"the two rims of one bore should measure the same: {spans}"


# THE WHOLE LOOP, AUTONOMOUS, AGAINST THE REAL KERNEL


def test_a_broken_file_is_inspected_repaired_and_promoted_without_anyone_asking(tmp_path,
                                                                               monkeypatch):
    """Inspect -> locate -> triage -> repair -> prove it stages -> mesh the repair.

    The acceptance this service exists for, on geometry OpenCASCADE genuinely objects to, with no
    operator in the loop. Every guard is live: the caps measure the result, promotion needs a real
    staging proof, and had any of them refused, the assertions below would see the original.
    """
    import asyncio

    from tests._geometry_support import geometry_state
    from tests.cad_fixtures import write_open_shell_step

    import meshpipeline.settings.cad_repair as cfg
    from meshpipeline.pipeline.repair_attempt import node_repair_attempt
    from meshpipeline.pipeline.repair_inspect import node_repair_inspect

    monkeypatch.setattr(cfg, "CAD_REPAIR_ENABLED", True)
    monkeypatch.setattr(cfg, "CAD_REPAIR_AUTONOMOUS", True)
    # the fixture's opening is one whole face of a cube, which the span cap rightly calls a missing
    # wall; this test is about the loop, so patching is allowed to reach it
    monkeypatch.setattr(cfg, "CAD_REPAIR_FILL_PLANAR_HOLES", True)
    monkeypatch.setattr(cfg, "CAD_REPAIR_MAX_HOLE_SPAN_RATIO", 1.0)

    write_open_shell_step(tmp_path / "part.step")
    state = {"job_id": "native-loop", "engine": "gmsh",
             "geometry": geometry_state(tmp_path, filename="part.step")}
    # geometry_state rewrites the file it is handed, so author the broken part over it afterwards
    write_open_shell_step(tmp_path / "part.step")
    original_sha = state["geometry"]["ref"]["sha256"]

    # 1: the inspection locates the opening
    state.update(asyncio.run(node_repair_inspect(state)))
    assert state["repair_status"] == "repairable", state["repair_status"]
    located = state["repair_report"]["report"]["entities"]
    assert [e["code"] for e in located] == ["open_shell"]

    # 2: the run repairs it and promotes the result, with nobody asked
    out = asyncio.run(node_repair_attempt(state))

    assert out["repair_attempt"]["attempted"] is True, out["repair_attempt"]
    # ATTEMPTED AND KEPT, not merely logged: the log also carries rolled-back attempts.
    fills = [op for op in out["repair_attempt"]["operations"] if op["name"] == "fill_planar_hole"]
    assert [op["status"] for op in fills] == ["kept"], out["repair_attempt"]["operations"]
    # THE RUN NOW MESHES THE REPAIR, and can still say which bytes it started from
    assert out["geometry"]["ref"]["sha256"] != original_sha
    assert out["repair_lineage"]["original"]["sha256"] == original_sha
    assert out["repair_lineage"]["engine_staged_for"] == "gmsh"

    # 3: and the repaired file really is repaired - proved by re-inspecting what was promoted
    from pathlib import Path as _Path

    from meshpipeline.cad.repair.brep import inspect_brep_file
    after = inspect_brep_file(_Path(out["geometry"]["local_path"]))
    assert after.entities == (), f"still located: {after.entities}"
    assert {m.name: m.value for m in after.measurements}["is_valid"] is True


def test_the_customers_upload_is_still_intact_after_an_autonomous_repair(tmp_path, monkeypatch):
    import asyncio

    from tests._geometry_support import geometry_state
    from tests.cad_fixtures import write_open_shell_step

    import meshpipeline.settings.cad_repair as cfg
    from meshpipeline.pipeline.repair_attempt import node_repair_attempt
    from meshpipeline.pipeline.repair_inspect import node_repair_inspect

    monkeypatch.setattr(cfg, "CAD_REPAIR_ENABLED", True)
    monkeypatch.setattr(cfg, "CAD_REPAIR_AUTONOMOUS", True)
    monkeypatch.setattr(cfg, "CAD_REPAIR_FILL_PLANAR_HOLES", True)
    monkeypatch.setattr(cfg, "CAD_REPAIR_MAX_HOLE_SPAN_RATIO", 1.0)

    state = {"job_id": "native-intact", "engine": "gmsh",
             "geometry": geometry_state(tmp_path, filename="part.step")}
    source = write_open_shell_step(tmp_path / "part.step")
    before = source.read_bytes()

    state.update(asyncio.run(node_repair_inspect(state)))
    asyncio.run(node_repair_attempt(state))

    # THE ONE INVARIANT THIS SERVICE RESTS ON: whatever we did, their bytes are still theirs.
    assert source.read_bytes() == before


def test_a_loose_body_beside_a_solid_is_sewn_without_costing_the_solid(tmp_path):
    """Both whole-shape answers are wrong on a mixed assembly, so sewing is judged per body.

    Sew the assembly and the solid is destroyed - a real-kernel test already caught that. Skip the
    assembly and the body that arrived as unjoined faces stays unjoined, and the customer gets a
    file called repaired that still will not mesh. This proves the third answer: the solid is
    handed back untouched and the loose body is joined.
    """
    from tests.cad_fixtures import write_mixed_assembly_step

    source = write_mixed_assembly_step(tmp_path / "mixed.step")
    report = repair_step_file(source, tmp_path / "mixed-repaired.step")

    measured = {m.name: m.value for m in report.measurements}
    before, after = measured["before"], measured["after"]

    assert before["solids"] == 1 and before["shells"] == 7, before
    # the loose body's six single-face shells became one, and the solid body kept its own. The
    # loop also PROMOTES the sewn closed shell to a solid, so the assembly ends with two.
    assert after["shells"] == 2, after
    assert after["solids"] == before["solids"] + 1, "the original solid was lost or not promoted"
    # NOTHING ELSE MOVED. Sewing joins what is already coincident, so the faces and the measured
    # geometry are identical - anything else here would be the customer's part being changed.
    assert after["faces"] == before["faces"]
    assert abs(after["volume_mm3"] - before["volume_mm3"]) < 1e-6
    assert abs(after["surface_area_mm2"] - before["surface_area_mm2"]) < 1e-6
    # and the report says sewing happened, because it did
    assert "sew_faces" in [op["name"] for op in report.operations if op["mutated"]]


def test_a_sound_single_solid_is_not_reported_as_sewn(tmp_path):
    source = write_step(tmp_path / "box.step", "MM")

    with pytest.raises(RepairRefused) as refused:
        repair_step_file(source, tmp_path / "box-repaired.step")

    # the one thing sewing must never touch, and nothing may claim it did
    assert refused.value.measurements["operations_attempted"] == 0


def test_sewing_bare_faces_is_reported_even_though_the_shell_count_rises():
    """The regression: sewing was inferred from a shell count, and bare faces have no shell.

    A body can arrive as individual faces with no shell around them at all. Sewing them produces
    one shell, so the count goes UP - and a report that decided "did sewing run?" by looking for a
    count that went down left the operation out, over a repaired file whose topology had changed.
    """
    from OCP.BRep import BRep_Builder
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS, TopoDS_Compound

    from meshpipeline.cad.repair.conservative import _counts, _sew_loose_bodies

    builder = BRep_Builder()
    assembly = TopoDS_Compound()
    builder.MakeCompound(assembly)
    builder.Add(assembly, BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape())

    bare = TopoDS_Compound()
    builder.MakeCompound(bare)
    loose = BRepPrimAPI_MakeBox(gp_Pnt(20.0, 0.0, 0.0), gp_Pnt(30.0, 10.0, 10.0)).Shape()
    explorer = TopExp_Explorer(loose, TopAbs_FACE)
    while explorer.More():
        builder.Add(bare, TopoDS.Face_s(explorer.Current()))
        explorer.Next()
    builder.Add(assembly, bare)

    before = _counts(assembly)
    assert before["solids"] == 1 and before["shells"] == 1, before   # the solid's own shell only

    joined, sewed = _sew_loose_bodies(assembly, tolerance_mm=0.1)
    after = _counts(joined)

    assert after["shells"] > before["shells"], "the bare faces were not joined into a shell"
    assert sewed is True, "a change the operator can see was left out of the report"
    assert after["solids"] == 1, "sewing cost the assembly its solid"


def test_the_report_states_what_the_loop_did_and_why_it_stopped(tmp_path, monkeypatch):
    from tests.cad_fixtures import write_loose_faces_step

    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_AREA_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_OPS", 6)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_STALL_LIMIT", 2)

    # THE LOOSE-FACES FIXTURE, because it is the one fixture this loop actually repairs.
    source = write_loose_faces_step(tmp_path / "loose.step")
    report = repair_step_file(source, tmp_path / "out.step")

    measured = {m.name: m.value for m in report.measurements}
    # WHY THE LOOP STOPPED IS PART OF THE REPORT. An operator approving a repair needs to know
    # whether it finished or ran out of room, and those look identical without this.
    assert measured["stopped_because"]
    assert isinstance(measured["resolved"], bool)
    assert isinstance(measured["complete_scan"], bool)
    assert measured["planner"] == "rules-v1"
    # EVERY ATTEMPT, KEPT OR NOT.
    assert report.operations, "the report lists no operations"
    for op in report.operations:
        assert op["name"] and op["status"] in ("kept", "rolled_back", "failed")
        assert "targets" in op and "delta" in op and "basis" in op
    assert measured["operations_attempted"] >= measured["operations_kept"] >= 1
    # THE OLD KEYS ARE GONE.
    assert "holes_filled" not in measured and "holes_left" not in measured


def test_the_reports_entities_are_what_is_left_rather_than_what_arrived(tmp_path, monkeypatch):
    from tests.cad_fixtures import write_loose_faces_step

    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_AREA_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO", 0.02)

    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.repair.localize import localize_scan

    source = write_loose_faces_step(tmp_path / "loose.step")
    arrived, _ = localize_scan(_read_shape(source))
    assert arrived, "the fixture is not broken"

    report = repair_step_file(source, tmp_path / "out.step")

    # An operator reading "repaired" over the ORIGINAL defect list is how a ruined delivery
    # happens. The delivered report states what remains.
    assert len(report.entities) < len(arrived)


def test_a_summary_never_claims_a_repair_the_run_did_not_achieve(tmp_path, monkeypatch):
    # The sliver fixture ends `stalled` with the sliver still present. The summary must not read
    # as success - `resolved` is False, and the wording has to follow it.
    from tests.cad_fixtures import write_small_edge_step

    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_AREA_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO", 0.02)

    out = tmp_path / "out.step"
    with pytest.raises(RepairRefused) as refused:
        repair_step_file(write_small_edge_step(tmp_path / "sliver.step"), out)

    measured = refused.value.measurements
    assert measured["resolved"] is False
    # THE STRONGEST FORM OF "DO NOT CLAIM A REPAIR YOU DID NOT MAKE": with nothing kept there is
    # no report to word carefully and no file to promote.
    assert measured["operations_kept"] == 0
    assert "nothing was kept" in str(refused.value)
    assert not out.exists()


def test_a_sound_part_reports_resolved_with_no_operations(tmp_path, monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_AREA_CHANGE_RATIO", 0.02)
    monkeypatch.setattr(rcfg, "CAD_REPAIR_MAX_VOLUME_CHANGE_RATIO", 0.02)

    with pytest.raises(RepairRefused) as refused:
        repair_step_file(write_step(tmp_path / "box.step", "MM"), tmp_path / "out.step")

    # RESOLVED AND REFUSED ARE NOT A CONTRADICTION: the loop had nothing to do and says so, and
    # the refusal is about there being nothing to DELIVER - the run keeps the customer's file.
    measured = refused.value.measurements
    assert measured["resolved"] is True
    assert measured["operations_attempted"] == 0, "a sound part had operations run on it"


def test_a_run_that_kept_nothing_is_refused_rather_than_delivered(tmp_path):
    """Nothing kept means nothing to promote, and a re-export is not a repair.

    The open-shell fixture's opening is a missing WALL (0.8165 of the diagonal, over the 0.75
    span cap), so filling declines it and no other operation can close it: the run keeps zero
    operations. Writing the STEP anyway handed the pipeline a freshly exported file - which
    `promote` accepts, because it refuses only byte-identical geometry - and `status_for` called
    it `repaired` because no error-severity defect was in the report. A STEP round trip changes
    topology, so that is the customer's upload replaced by a different shape for no repair at all.
    """
    from tests.cad_fixtures import write_open_shell_step

    source = write_open_shell_step(tmp_path / "holed.step")
    out = tmp_path / "holed-repaired.step"

    with pytest.raises(RepairRefused) as refused:
        repair_step_file(source, out)

    assert "nothing was kept" in str(refused.value)
    assert refused.value.measurements["operations_kept"] == 0
    # AND THE ATTEMPTS TRAVEL WITH THE REFUSAL. An attempted-and-rolled-back sequence is the
    # training signal slice 8 most needs, and this run produces nothing else; a decline that
    # dropped it would delete the corpus row for every file the loop cannot repair.
    attempts = refused.value.measurements["operations"]
    assert len(attempts) == refused.value.measurements["operations_attempted"] >= 1
    for op in attempts:
        assert op["name"] and op["mutated"] is False
        assert op["status"] in ("rolled_back", "failed") and op["reason"]
        assert "targets" in op and "delta" in op and "basis" in op
    # REFUSED MEANS NOTHING WAS DELIVERED. The node leaves the run on the original upload, which
    # is the same outcome as not having tried.
    assert not out.exists()


@pytest.mark.parametrize("unit, expected", [("INCH", "in"), ("M", "m"), ("MM", "mm")])
def test_a_repaired_file_declares_the_uploads_unit_so_the_carried_interpretation_still_holds(
        tmp_path, unit, expected):
    """Promotion carries the upload's interpretation onto the repaired bytes, and staging reads it
    RELATIVE TO THE UNIT THE FILE DECLARES (coordinate_state.to_metres). A repaired file declaring
    millimetres under an inch upload's interpretation was staged 25.4 times too large; a metre one,
    1000 times. So the repaired file declares the unit the upload declared, and the part reads back
    the same physical size."""
    from tests.cad_fixtures import write_loose_faces_step

    from meshpipeline.cad.repair.brep import _read_shape
    from meshpipeline.cad.repair.localize import _diagonal
    from meshpipeline.cad.unit_evidence import parser_applied_unit

    source = write_loose_faces_step(tmp_path / "part.step", unit)
    repaired = tmp_path / "part-repaired.step"
    repair_step_file(source, repaired)

    assert parser_applied_unit(source).value == expected
    assert parser_applied_unit(repaired) == parser_applied_unit(source)
    before, after = _diagonal(_read_shape(source)), _diagonal(_read_shape(repaired))
    assert abs(after - before) / before < 1e-6, (before, after)
