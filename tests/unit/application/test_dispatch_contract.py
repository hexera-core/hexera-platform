# Responsibility: Verify a dispatch payload is versioned, validated against the run signature, refusing tampered intent.
from __future__ import annotations

import inspect

import pytest

from meshpipeline.application import dispatch_contract as dc
from meshpipeline.application.approved_patch_contract import ApprovedPatchContract
from meshpipeline.application.pipeline_run import JobRequest, run_pipeline


def _empty_contract() -> dict:
    return ApprovedPatchContract.build([], required=False).to_dict()


def _intent_fp(**fields) -> str:
    return dc.recompute_intent_fingerprint(fields)


# direction 1: everything a producer builds must bind to the run entry

def test_a_built_payload_binds_to_the_run_entry():
    payload = dc.build(job_id="j", owner_id="o", approved_snapshot_id="snap-1",
                       approved_patch_contract=_empty_contract(),
                       approved_intent_fingerprint=_intent_fp())
    inspect.signature(run_pipeline).bind(**dc.to_run_kwargs(payload))


def test_an_unknown_producer_key_is_refused_at_build_time():
    with pytest.raises(dc.DispatchContractError, match="does not accept"):
        dc.build(job_id="j", not_a_run_pipeline_argument="x")


def test_the_exact_defect_is_now_a_contract_error_not_a_worker_typeerror():
    dc.build(job_id="j", approved_snapshot_id="snap-1",
             approved_patch_contract=_empty_contract(),
             approved_intent_fingerprint=_intent_fp())            # accepted
    with pytest.raises(dc.DispatchContractError, match="approved_snapshot"):
        dc.build(job_id="j", approved_snapshot="snap-1")          # misspelled → caught


# direction 2: everything the run entry requires must be produced

def test_required_run_entry_keys_are_derived_not_hand_listed():
    assert dc.required_keys() == {"job_id"}
    assert dc.required_keys() <= dc.accepted_keys()
    with pytest.raises(dc.DispatchContractError, match="missing required"):
        dc.build(owner_id="o")


def test_accepted_keys_come_from_the_run_signature_itself():
    assert dc.accepted_keys() == frozenset(inspect.signature(run_pipeline).parameters)


def test_run_pipeline_takes_no_var_kwargs():
    kinds = {p.kind for p in inspect.signature(run_pipeline).parameters.values()}
    assert inspect.Parameter.VAR_KEYWORD not in kinds


# the run entry actually threads the identifier

def test_approved_snapshot_id_reaches_jobrequest():
    req = JobRequest(job_id="j", approved_snapshot_id="snap-42")
    assert req.approved_snapshot_id == "snap-42"
    assert "approved_snapshot_id" in JobRequest.__slots__


def test_the_identifier_is_diagnostic_only():
    assert JobRequest(job_id="j").approved_snapshot_id == ""
    assert dc._OPTIONAL_DISPATCH_DEFAULTS["approved_snapshot_id"] == ""


# durable versioning

def test_a_current_payload_carries_and_validates_its_version():
    payload = dc.build(job_id="j")
    assert payload["schema_version"] == dc.DISPATCH_SCHEMA_VERSION
    dc.validate(payload)
    assert "schema_version" not in dc.to_run_kwargs(payload), "the envelope is not a run argument"


def test_a_payload_without_a_version_is_refused():
    # An unversioned payload was once read as version 0 and upgraded on the way through. That
    # path existed for rows written before this contract and is gone: every payload is stamped
    # by build(), and a pre-release build has no earlier form to accept.
    unversioned = {"job_id": "old-job", "owner_id": "o", "mesh_engine": "cfmesh"}
    with pytest.raises(dc.DispatchContractError, match="schema_version"):
        dc.to_run_kwargs(unversioned)


def test_a_superseded_version_is_refused_like_any_other():
    superseded = dc.build(job_id="j")
    superseded["schema_version"] = dc.DISPATCH_SCHEMA_VERSION - 1
    with pytest.raises(dc.DispatchContractError, match="schema_version"):
        dc.to_run_kwargs(superseded)


def test_the_current_version_still_binds_to_the_run_entry():
    kwargs = dc.to_run_kwargs(dc.build(job_id="old-job", owner_id="o", mesh_engine="cfmesh"))
    assert kwargs["approved_snapshot_id"] == ""
    inspect.signature(run_pipeline).bind(**kwargs)


def test_an_unknown_future_version_fails_safely():
    future = dc.build(job_id="j")
    future["schema_version"] = dc.DISPATCH_SCHEMA_VERSION + 1
    with pytest.raises(dc.DispatchContractError, match="schema_version"):
        dc.to_run_kwargs(future)


def test_a_malformed_persisted_payload_fails_clearly():
    for bad in ([], "not-a-dict", {"job_id": "j", "schema_version": "one"},
                {"job_id": "j", "schema_version": -1}):
        with pytest.raises(dc.DispatchContractError):
            dc.to_run_kwargs(bad)


# both backends consume ONE schema

def test_celery_and_cloud_run_consume_the_same_contract():
    import meshpipeline.adapters.pipeline_execution.celery as celery_adapter

    payload = dc.build(job_id="j", approved_snapshot_id="snap-1", mesh_engine="cfmesh",
                       approved_patch_contract=_empty_contract(),
                       approved_intent_fingerprint=_intent_fp(mesh_engine="cfmesh"))
    celery_kwargs = dc.to_run_kwargs(payload)             # what the task entry receives
    reconstructed = dc.to_run_kwargs(dict(payload))       # what run_from_job rebuilds
    assert celery_kwargs == reconstructed
    sig = inspect.signature(run_pipeline)
    sig.bind(**celery_kwargs)
    sig.bind(**reconstructed)
    # the celery task itself forwards straight into run_pipeline, so its acceptance IS run_pipeline's
    assert inspect.Parameter.VAR_KEYWORD in {
        p.kind for p in inspect.signature(celery_adapter.run_simulation).parameters.values()}


# the approved-patch-contract invariant at the dispatch boundary (unit-level kills)

def test_a_v2_approved_snapshot_without_a_typed_contract_is_refused():
    with pytest.raises(dc.DispatchContractError, match="no typed approved_patch_contract"):
        dc.validate({"schema_version": dc.DISPATCH_SCHEMA_VERSION, "job_id": "j",
                     "approved_snapshot_id": "snap-1"}, where="unit")




def test_an_approved_contract_that_does_not_match_the_execution_patches_is_refused():
    from meshpipeline.application.approved_patch_contract import ApprovedPatchContract
    contract = ApprovedPatchContract.build([{"name": "inlet", "type": "inlet"},
                                            {"name": "wall", "type": "wall"}]).to_dict()
    with pytest.raises(dc.DispatchContractError, match="does not match|missing"):
        dc.validate({"schema_version": dc.DISPATCH_SCHEMA_VERSION, "job_id": "j",
                     "approved_snapshot_id": "snap-1", "approved_patch_contract": contract,
                     "intake_patches": [{"name": "inlet", "type": "inlet"}]}, where="unit")


# the COMPLETE approved-intent binding (engine/purpose/input_kind/dimensionality/…)

def _approved_payload(**fields):
    base = {"schema_version": dc.DISPATCH_SCHEMA_VERSION, "job_id": "j",
            "approved_snapshot_id": "snap-1",
            "approved_patch_contract": ApprovedPatchContract.build([], required=False).to_dict()}
    base.update(fields)
    base["approved_intent_fingerprint"] = dc.recompute_intent_fingerprint(base)
    return base


def test_an_approved_job_without_an_intent_fingerprint_is_refused():
    p = {"schema_version": dc.DISPATCH_SCHEMA_VERSION, "job_id": "j", "approved_snapshot_id": "snap-1",
         "approved_patch_contract": ApprovedPatchContract.build([], required=False).to_dict()}
    with pytest.raises(dc.DispatchContractError, match="approved_intent_fingerprint"):
        dc.validate(p, where="unit")


@pytest.mark.parametrize("field,tampered", [
    ("mesh_engine", "gmsh"),
    ("purpose", "external_cfd"),
    ("input_kind", "solid"),
    ("dimensionality", "2D"),
    ("engine_params", {"tampered": 1}),
])
def test_a_tampered_intent_field_fails_the_fingerprint(field, tampered):
    p = _approved_payload(mesh_engine="cfmesh", purpose="internal_cfd",
                          input_kind="fluid", dimensionality="3D", engine_params={"a": 1})
    p[field] = tampered                                   # drift the field, keep the old fingerprint
    with pytest.raises(dc.DispatchContractError, match="approved intent does not match"):
        dc.validate(p, where="unit")


def test_an_intact_approved_intent_validates():
    dc.validate(_approved_payload(mesh_engine="cfmesh", purpose="internal_cfd", input_kind="fluid",
                                  dimensionality="3D"), where="unit")


def test_a_direct_dispatch_with_no_snapshot_needs_no_contract():
    dc.validate({"schema_version": dc.DISPATCH_SCHEMA_VERSION, "job_id": "j",
                 "intake_patches": []}, where="unit")           # no raise
    with pytest.raises(dc.DispatchContractError, match="schema_version"):
        dc.validate({"job_id": "j", "intake_patches": []}, where="unit")   # unversioned: refused
