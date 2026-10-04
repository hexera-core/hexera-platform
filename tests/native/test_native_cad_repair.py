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
    from tests._geometry_support import geometry_state

    from meshpipeline.contracts.geometry_source import (
        GeometrySourceRef,
        MaterializedGeometry,
        sha256_of,
    )
    from meshpipeline.pipeline.geometry_state import materialized
    from meshpipeline.pipeline.repair_promote import derived_interpretation, promote

    source = write_step(tmp_path / "part.step", "MM")
    state = {"job_id": "native-1", "engine": "gmsh",
             "geometry": geometry_state(tmp_path, filename="part.step")}
    original = materialized(state)

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
