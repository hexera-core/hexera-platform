# Responsibility: Verify the delivery manifest states only what the durable rows support, and names what is missing before a parcel is handed over.
from __future__ import annotations

from datetime import UTC, datetime

from meshpipeline.application.delivery_manifest import (
    DELIVERY_MANIFEST_VERSION,
    build,
    evidence_gaps,
)
from meshpipeline.persistence.models import ArtifactType

_WHEN = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


class _Row:
    def __init__(self, artifact_type, *, logical_key=None, checksum="abc123", size=1024,
                 attempt=0, generation=1):
        self.artifact_type = artifact_type
        self.logical_key = logical_key or artifact_type.value
        self.storage_key = f"jobs/j1/{self.logical_key}"
        self.checksum = checksum
        self.size_bytes = size
        self.delivery_attempt = attempt
        self.execution_generation = generation
        self.created_at = _WHEN


def _delivered(**over) -> dict:
    return {"status": "succeeded", "outcome_code": "success", "required_ready": True, **over}


def test_the_manifest_lists_every_artifact_with_its_integrity_claim():
    manifest = build(job_id="j1", artifacts=[_Row(ArtifactType.mesh_bundle)],
                     final_result=_delivered())

    assert manifest["manifest_version"] == DELIVERY_MANIFEST_VERSION
    entry = manifest["artifacts"][0]
    assert entry["class"] == "mesh_bundle"
    assert entry["checksum"] == "abc123" and entry["size_bytes"] == 1024
    # so a customer or a support engineer can prove the file they hold is the file we delivered
    assert entry["storage_key"] == "jobs/j1/mesh_bundle"
    assert entry["delivered_at"] == _WHEN.isoformat()


def test_the_viewer_payload_is_delivered_but_never_offered_as_a_download():
    manifest = build(job_id="j1", artifacts=[_Row(ArtifactType.mesh_bundle),
                                             _Row(ArtifactType.viewer_data)],
                     final_result=_delivered())

    # it is what the VIEWER consumes; listing it would put an internal JSON blob in the
    # deliverables panel beside the engine case
    assert "viewer_data" in manifest["delivered_classes"]
    assert [e["class"] for e in manifest["customer_visible"]] == ["mesh_bundle"]


def test_the_repaired_file_is_offered_to_the_customer():
    manifest = build(job_id="j1", artifacts=[_Row(ArtifactType.mesh_bundle),
                                             _Row(ArtifactType.repaired_cad),
                                             _Row(ArtifactType.repair_report)],
                     final_result=_delivered())

    # they paid for the fixed geometry as much as for the mesh built from it
    assert {e["class"] for e in manifest["customer_visible"]} == {
        "mesh_bundle", "repaired_cad", "repair_report"}


def test_the_manifest_says_which_bytes_the_mesh_was_built_from():
    lineage = {"original": {"source_id": "s1", "sha256": "a" * 64, "size_bytes": 10},
               "repaired": {"source_id": "s2", "sha256": "b" * 64, "size_bytes": 11},
               "engine_staged_for": "snappy"}
    manifest = build(job_id="j1", artifacts=[_Row(ArtifactType.mesh_bundle)],
                     final_result=_delivered(repair_lineage=lineage))

    # "we fixed your file and meshed the fix" is a different claim from "we meshed your file"
    assert manifest["built_from_repaired_geometry"] is True
    assert manifest["source_identity"]["sha256"] == "a" * 64
    assert manifest["repair_lineage"]["engine_staged_for"] == "snappy"


def test_a_job_that_meshed_the_upload_claims_no_repair():
    manifest = build(job_id="j1", artifacts=[_Row(ArtifactType.mesh_bundle)],
                     final_result=_delivered())
    assert manifest["built_from_repaired_geometry"] is False
    assert manifest["repair_lineage"] == {} and manifest["source_identity"] == {}


def test_every_caveat_the_result_carries_is_in_the_parcel_too():
    manifest = build(job_id="j1", artifacts=[_Row(ArtifactType.mesh_bundle)],
                     final_result=_delivered(
                         requirement_caveats=[{"key": "wake_length", "measured": "4.2D"}],
                         optional_warnings=["mesh"]))

    # a customer reading what they were given must not have to find the caveats somewhere else
    assert manifest["requirement_caveats"][0]["key"] == "wake_length"
    assert manifest["optional_warnings"] == ["mesh"]


def test_readiness_is_taken_from_the_terminal_record_not_recomputed():
    # two authorities on whether a job delivered is one too many: the manifest repeats the
    # record's answer even when the rows might suggest another
    manifest = build(job_id="j1", artifacts=[_Row(ArtifactType.mesh_bundle)],
                     final_result={"required_ready": False, "missing_outputs": ["viewer_data"]})
    assert manifest["required_ready"] is False
    assert manifest["missing_outputs"] == ["viewer_data"]


def test_a_manifest_with_no_result_claims_nothing():
    manifest = build(job_id="j1", artifacts=[], final_result=None)
    assert manifest["required_ready"] is False
    assert manifest["artifacts"] == [] and manifest["delivered_classes"] == []
    assert manifest["built_from_repaired_geometry"] is False


def test_a_malformed_lineage_is_ignored_rather_than_crashing_a_read():
    # an older or hand-edited record must never break the page that shows a customer their files
    manifest = build(job_id="j1", artifacts=[], final_result={"repair_lineage": "not-a-mapping"})
    assert manifest["repair_lineage"] == {}
    assert manifest["built_from_repaired_geometry"] is False


# WHAT IS MISSING BEFORE WE HAND IT OVER


def test_a_complete_ordinary_delivery_has_no_gaps():
    manifest = build(job_id="j1", artifacts=[_Row(ArtifactType.mesh_bundle),
                                             _Row(ArtifactType.viewer_data)],
                     final_result=_delivered())
    assert evidence_gaps(manifest) == []


def test_a_mesh_that_never_arrived_is_a_gap():
    manifest = build(job_id="j1", artifacts=[], final_result={"required_ready": False})
    assert any("required mesh deliverables" in g for g in evidence_gaps(manifest))


def test_a_repaired_delivery_without_the_repaired_file_is_incomplete():
    lineage = {"original": {"source_id": "s1", "sha256": "a" * 64}}
    manifest = build(job_id="j1", artifacts=[_Row(ArtifactType.mesh_bundle)],
                     final_result=_delivered(repair_lineage=lineage))

    gaps = evidence_gaps(manifest)
    # the mesh arrived, so `required_ready` is satisfied - but a service that fixed a customer's
    # CAD and will not give it back has delivered half the work
    assert manifest["required_ready"] is True
    assert any("cannot download" in g for g in gaps)
    assert any("no report explains what changed" in g for g in gaps)


def test_a_repaired_delivery_with_its_evidence_is_complete():
    lineage = {"original": {"source_id": "s1", "sha256": "a" * 64}}
    manifest = build(job_id="j1",
                     artifacts=[_Row(ArtifactType.mesh_bundle), _Row(ArtifactType.repaired_cad),
                                _Row(ArtifactType.repair_report)],
                     final_result=_delivered(repair_lineage=lineage))
    assert evidence_gaps(manifest) == []


def test_a_repaired_delivery_that_cannot_name_the_original_is_incomplete():
    manifest = build(job_id="j1",
                     artifacts=[_Row(ArtifactType.mesh_bundle), _Row(ArtifactType.repaired_cad),
                                _Row(ArtifactType.repair_report)],
                     final_result=_delivered(repair_lineage={"engine_staged_for": "snappy"}))
    assert any("does not identify the original upload" in g for g in evidence_gaps(manifest))
