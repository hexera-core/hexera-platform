# Responsibility: Verify every dispatch field is classified as fingerprinted or excluded, each exclusion with a reason.
from __future__ import annotations

from meshpipeline.agents.intake.admission_token import fingerprint
from meshpipeline.application.dispatch_contract import accepted_keys, recompute_intent_fingerprint, source_ref_of

# the fields whose values the approved-intent fingerprint canonicalizes
# `mesh_fidelity` is the TYPED approved fidelity (never inferred from prose); `request_txt`
# contributes the `approved_request_digest` PROVENANCE field; `geometry_source` contributes the
# geometry's CONTENT identity (never the path itself). See test_approved_intent_binding.py.
FINGERPRINTED = {
    "mesh_engine", "purpose", "input_kind", "dimensionality", "intake_patches", "engine_params",
    "requested_mesh_fidelity", "effective_mesh_fidelity", "mesh_fidelity_source",
    "fidelity_policy_version", "request_txt", "geometry_source",
    # the same bytes read as millimetres and as metres are different approvals,
    # because they mesh to physically different objects
    "geometry_interpretation",
}

# run-determining, but bound by a SEPARATE typed contract that is itself verified
SEPARATELY_BOUND = {
    # the EXACT boundary set, bound by ApprovedPatchContract (built once from the approved snapshot,
    # re-verified at reconstruction AND at graph admission) - and also inside the fingerprint above
    "intake_patches",
    # the fingerprint itself + the snapshot it points at
    "approved_intent_fingerprint", "approved_snapshot_id", "approved_patch_contract",
}

# NOT run-determining: identity/plumbing, or derived downstream
ENVELOPE = {
    "job_id", "owner_id", "session_id", "intake_events", "domain",
}

# DECLARED EXCLUSIONS: run-determining but deliberately outside the fingerprint, each with the
#    reason and the mitigation that makes the exclusion safe at the approval→dispatch boundary.
DECLARED_EXCLUSIONS: dict[str, str] = {
    "review_brief_txt": (
        "The review brief. It steers REVIEW, not what is meshed. MITIGATION: it is taken verbatim "
        "from the durable approved snapshot, and it cannot authorize a delivery on its own - the "
        "review rubric is engine/purpose-owned data (both fingerprinted), and success additionally "
        "requires executor_success (the engine's own gates) plus a delivered required artifact, so "
        "a tampered brief cannot turn an unvalidated mesh into a shipped one."),
    "user_dispute": (
        "A dispute run is a DIFFERENT logical run against an already-delivered mesh; it carries its "
        "parent's pinned engine and is re-reviewed, not re-approved through the intake gate."),
}


def test_every_dispatch_field_is_classified():
    classified = FINGERPRINTED | SEPARATELY_BOUND | ENVELOPE | set(DECLARED_EXCLUSIONS)
    unclassified = sorted(accepted_keys() - classified)
    assert not unclassified, (
        f"dispatch field(s) not classified against the approved-intent fingerprint: {unclassified}. "
        "Add each to FINGERPRINTED, SEPARATELY_BOUND, ENVELOPE, or DECLARED_EXCLUSIONS (with a "
        "written reason + mitigation).")


def test_no_classification_is_stale():
    classified = FINGERPRINTED | SEPARATELY_BOUND | ENVELOPE | set(DECLARED_EXCLUSIONS)
    gone = sorted(classified - accepted_keys())
    assert not gone, f"classified but no longer a dispatch field: {gone}"


def test_every_declared_exclusion_carries_a_reason():
    for field, reason in DECLARED_EXCLUSIONS.items():
        assert len(reason) > 80, f"{field}: an exclusion needs a real written justification"
        assert "MITIGATION" in reason or "DIFFERENT logical run" in reason, (
            f"{field}: an exclusion must state what makes it safe")


# the fingerprint really does cover what it claims
def _payload(**over):
    base = {"mesh_engine": "cfmesh", "purpose": "external_cfd", "input_kind": "solid",
            "dimensionality": "3d", "intake_patches": [{"name": "inlet", "role": "inlet"}],
            "engine_params": {"cells": 1},
            "requested_mesh_fidelity": "draft", "effective_mesh_fidelity": "draft",
            "mesh_fidelity_source": "user", "fidelity_policy_version": "3tier-v1"}
    base.update(over)
    return base


def test_each_fingerprinted_field_actually_changes_the_fingerprint():
    base = recompute_intent_fingerprint(_payload())
    changes = {
        "mesh_engine": "snappy", "purpose": "internal_cfd", "input_kind": "surface",
        "dimensionality": "2d", "intake_patches": [{"name": "outlet", "role": "outlet"}],
        "engine_params": {"cells": 2},
        # the fidelity fields move together (an inconsistent trio is refused by design) and are
        # covered by test_the_four_fidelity_fields_each_change_the_fingerprint
    }
    for field, altered in changes.items():
        assert recompute_intent_fingerprint(_payload(**{field: altered})) != base, (
            f"{field} is declared FINGERPRINTED but does not change the approved-intent fingerprint")


def test_patch_order_does_not_matter_but_identity_and_role_do():
    a = recompute_intent_fingerprint(_payload(intake_patches=[
        {"name": "inlet", "role": "inlet"}, {"name": "outlet", "role": "outlet"}]))
    b = recompute_intent_fingerprint(_payload(intake_patches=[
        {"name": "outlet", "role": "outlet"}, {"name": "inlet", "role": "inlet"}]))
    assert a == b                                        # order is not intent
    c = recompute_intent_fingerprint(_payload(intake_patches=[
        {"name": "inlet", "role": "outlet"}, {"name": "outlet", "role": "inlet"}]))
    assert c != a                                        # a RE-ROLED boundary is different intent


def test_a_declared_exclusion_does_not_change_the_fingerprint():
    base = recompute_intent_fingerprint(_payload())
    for field, value in (("review_brief_txt", "look hard at the trailing edge"),
                         ("user_dispute", {"flags": [], "comment": "x"})):
        assert recompute_intent_fingerprint(_payload(**{field: value})) == base, (
            f"{field} now affects the fingerprint - move it out of DECLARED_EXCLUSIONS into "
            "FINGERPRINTED and update the reasons")


# the two questions the audit asked, answered in an executable form
def test_the_four_fidelity_fields_each_change_the_fingerprint():
    base = recompute_intent_fingerprint(_payload())
    assert recompute_intent_fingerprint(_payload(
        requested_mesh_fidelity="max", effective_mesh_fidelity="max")) != base
    assert recompute_intent_fingerprint(_payload(
        requested_mesh_fidelity=None, effective_mesh_fidelity="standard",
        mesh_fidelity_source="default")) != base


def test_fidelity_is_an_explicit_typed_field_not_inferred_from_prose():
    from meshpipeline.pipeline.enums import MeshFidelity
    assert "requested_mesh_fidelity" in accepted_keys()   # a first-class run-entry parameter
    base = recompute_intent_fingerprint(_payload())
    assert recompute_intent_fingerprint(_payload(
        requested_mesh_fidelity="max", effective_mesh_fidelity="max")) != base
    # the envelope stores the readable typed value
    from meshpipeline.agents.intake.admission_token import approved_intent_canonical
    canon = approved_intent_canonical(
        engine="cfmesh", purpose="external_cfd", input_kind="solid", dimensionality="3d",
        patches=[], engine_params={}, requested_mesh_fidelity="max", request_txt="x",
        source_ref=None)
    assert canon["effective_mesh_fidelity"] == MeshFidelity.MAX.value
    assert canon["requested_mesh_fidelity"] == MeshFidelity.MAX.value
    assert "approved_request_digest" in canon


def test_required_output_is_engine_derived_not_an_approved_payload_field():
    assert not {k for k in accepted_keys() if "required_output" in k or "deliverable" in k}
    assert "mesh_engine" in FINGERPRINTED
    from meshpipeline.agents.intake.admission_token import required_output_classes
    assert required_output_classes("gmsh") == ["mesh_bundle", "viewer_data"]


def test_the_canonical_form_is_the_one_the_approval_record_uses():
    from meshpipeline.agents.intake.admission_token import approved_intent_canonical
    p = _payload()
    direct = fingerprint(approved_intent_canonical(
        engine=p["mesh_engine"], purpose=p["purpose"], input_kind=p["input_kind"],
        dimensionality=p["dimensionality"], patches=p["intake_patches"],
        engine_params=p["engine_params"],
        requested_mesh_fidelity=p["requested_mesh_fidelity"],
        effective_mesh_fidelity=p["effective_mesh_fidelity"],
        mesh_fidelity_source=p["mesh_fidelity_source"],
        fidelity_policy_version=p["fidelity_policy_version"],
        request_txt=p.get("request_txt", ""), source_ref=source_ref_of(p)))
    assert recompute_intent_fingerprint(p) == direct
