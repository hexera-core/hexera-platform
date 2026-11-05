# Responsibility: Verify the approved-intent fingerprint binds every field a run may not change, and refuses drift.
from __future__ import annotations

import dataclasses

import pytest
from tests._geometry_support import interpretation_ref, source_ref

import meshpipeline.agents.intake.admission_token as at
from meshpipeline.application.dispatch_contract import (
    DISPATCH_SCHEMA_VERSION,
    DispatchContractError,
    accepted_keys,
    recompute_intent_fingerprint,
    validate,
)
from meshpipeline.pipeline.enums import (
    DEFAULT_MESH_FIDELITY,
    FIDELITY_POLICY_VERSION,
    FidelityContractError,
    MeshFidelity,
    resolve_mesh_fidelity,
)

_PATCHES = [{"name": "inlet", "role": "inlet"}, {"name": "outlet", "role": "outlet"}]
_REQUEST = "mesh the duct for an internal flow study with resolved near-wall layers"


@pytest.fixture()
def geometry(tmp_path):
    return source_ref(tmp_path=tmp_path, filename="part.step", marker="approved solid")


def _approved(geometry, *, requested=None, request_txt=_REQUEST, **over):
    from meshpipeline.application.approved_patch_contract import ApprovedPatchContract
    req, eff, src = resolve_mesh_fidelity(requested)
    base = {
        # The envelope version is part of every persisted payload; the contract accepts exactly
        # the current one, so a fixture that omits it is refused before any binding is checked.
        "schema_version": DISPATCH_SCHEMA_VERSION,
        "job_id": "11111111-1111-1111-1111-111111111111", "owner_id": "o", "session_id": "s",
        # An approved run carries the bytes AND their physical scale - the dispatch contract
        # refuses one without the other, because size is not derivable from the source row.
        "geometry_source": geometry.to_payload(),
        "geometry_interpretation": interpretation_ref(
            geometry_source_id=geometry.source_id).to_payload(),
        "request_txt": request_txt,
        "review_brief_txt": "check the walls", "intake_patches": list(_PATCHES),
        "dimensionality": "3D", "purpose": "internal_cfd", "input_kind": "fluid-domain",
        "mesh_engine": "gmsh", "domain": "duct", "engine_params": {"element_order": "2"},
        "requested_mesh_fidelity": None if req is None else req.value,
        "effective_mesh_fidelity": eff.value,
        "mesh_fidelity_source": src.value,
        "fidelity_policy_version": FIDELITY_POLICY_VERSION,
        "approved_snapshot_id": "snap-1",
        "approved_patch_contract": ApprovedPatchContract.build(list(_PATCHES)).to_dict(),
    }
    base["approved_intent_fingerprint"] = recompute_intent_fingerprint(base)
    base.update(over)
    return base


def _refused(payload, match=""):
    with pytest.raises((DispatchContractError, FidelityContractError), match=match):
        validate(payload, where="reconstruction")


# the canonical vocabulary is exactly three tiers
def test_canonical_tiers_are_exactly_draft_standard_max():
    assert [m.value for m in MeshFidelity] == ["draft", "standard", "max"]
    assert "high" not in {m.value for m in MeshFidelity}
    assert DEFAULT_MESH_FIDELITY is MeshFidelity.STANDARD


def test_a_new_canonical_record_can_never_contain_high(geometry):
    for req in (None, "draft", "standard", "max"):
        canon = at.approved_intent_canonical(
            engine="gmsh", purpose="internal_cfd", input_kind="fluid-domain", dimensionality="3D",
            patches=_PATCHES, engine_params={}, requested_mesh_fidelity=req,
            request_txt=_REQUEST, source_ref=geometry)
        assert canon["effective_mesh_fidelity"] != "high"
        assert canon["requested_mesh_fidelity"] != "high"


def test_an_invalid_non_empty_tier_is_refused():
    with pytest.raises(FidelityContractError, match="not one of"):
        resolve_mesh_fidelity("ultra")


# omission is not a user selection
@pytest.mark.parametrize("omitted", [None, "", "   ", "\t"])
def test_omission_yields_default_provenance_and_never_blocks(geometry, omitted):
    req, eff, src = resolve_mesh_fidelity(omitted)
    assert req is None and eff is MeshFidelity.STANDARD and src.value == "default"
    validate(_approved(geometry, requested=omitted), where="reconstruction")


def test_omitted_and_explicit_standard_are_DIFFERENT_records(geometry):
    defaulted = _approved(geometry, requested=None)
    chosen = _approved(geometry, requested="standard")
    assert defaulted["effective_mesh_fidelity"] == chosen["effective_mesh_fidelity"] == "standard"
    assert defaulted["requested_mesh_fidelity"] is None
    assert chosen["requested_mesh_fidelity"] == "standard"
    assert defaulted["mesh_fidelity_source"] == "default"
    assert chosen["mesh_fidelity_source"] == "user"
    assert defaulted["approved_intent_fingerprint"] != chosen["approved_intent_fingerprint"]


# consistency rules fail closed at reconstruction
@pytest.mark.parametrize("bad", [
    {"requested_mesh_fidelity": None, "mesh_fidelity_source": "user"},
    {"requested_mesh_fidelity": "draft", "mesh_fidelity_source": "default"},
    {"requested_mesh_fidelity": "draft", "effective_mesh_fidelity": "max"},
    {"mesh_fidelity_source": "system"},
    {"effective_mesh_fidelity": "high"},
    # a canonical `high` in EITHER field is refused: high is not a new-record tier
    {"requested_mesh_fidelity": "high", "effective_mesh_fidelity": "high"},
    {"requested_mesh_fidelity": "high"},
])
def test_inconsistent_fidelity_combinations_fail_closed(geometry, bad):
    _refused(_approved(geometry, requested="draft", **bad))


def test_a_new_record_cannot_carry_canonical_high_in_any_field():
    from meshpipeline.pipeline.enums import FidelityContractError, assert_fidelity_consistent
    for kw in ({"requested": "high", "effective": "high", "source": "user"},
               {"requested": None, "effective": "high", "source": "default"}):
        with pytest.raises(FidelityContractError):
            assert_fidelity_consistent(policy_version="3tier-v1", **kw)


def test_policy_version_drift_is_detected(geometry):
    _refused(_approved(geometry, requested="draft", fidelity_policy_version="3tier-v2"))


# substitution mutations
@pytest.mark.parametrize("approved,executed", [
    ("draft", "standard"), ("standard", "max"), ("draft", "max"),
    ("max", "standard"), ("standard", "draft"), ("max", "draft"),
])
def test_mutation_every_fidelity_substitution_is_rejected(geometry, approved, executed):
    _refused(_approved(geometry, requested=approved,
                       requested_mesh_fidelity=executed, effective_mesh_fidelity=executed))


def test_mutation_source_substitution_is_rejected(geometry):
    p = _approved(geometry, requested=None)          # approved as a system default
    p["mesh_fidelity_source"] = "user"               # re-labelled as a user choice
    p["requested_mesh_fidelity"] = "standard"
    _refused(p)


def test_mutation_prose_unchanged_but_tier_changed_is_rejected(geometry):
    p = _approved(geometry, requested="draft", requested_mesh_fidelity="max",
                  effective_mesh_fidelity="max")
    assert p["request_txt"] == _REQUEST               # prose genuinely untouched
    _refused(p)


def test_mutation_prose_changed_but_tier_unchanged_follows_digest_policy(geometry):
    reworded = _approved(geometry)
    reworded["request_txt"] = _REQUEST + " and make the outlet longer"
    _refused(reworded)
    cosmetic = _approved(geometry)
    cosmetic["request_txt"] = "  mesh   the duct for an internal flow study with\r\n" \
                              "resolved near-wall layers  "
    validate(cosmetic, where="reconstruction")


def test_request_digest_omitted_does_not_remove_the_tier_binding(geometry):
    blank = _approved(geometry, requested="draft", request_txt="")
    validate(blank, where="reconstruction")
    assert at.approved_request_digest("") == ""
    drifted = dict(blank)
    drifted["requested_mesh_fidelity"] = "max"
    drifted["effective_mesh_fidelity"] = "max"
    _refused(drifted)


# geometry identity: structured and content-bound
def test_geometry_identity_is_structured_and_always_carries_the_checksum(geometry):
    ident = at.geometry_identity(geometry, revision_id="gen/1729")
    assert ident["identity_schema_version"] == at.GEOMETRY_IDENTITY_SCHEMA_VERSION
    assert ident["revision_id"] == "gen/1729"
    assert ident["sha256"] == geometry.sha256
    assert ident["size_bytes"] == geometry.size_bytes
    assert ident["source_id"] == geometry.source_id


def test_mutation_same_revision_id_with_changed_bytes_is_rejected(geometry):
    before = at.geometry_identity(geometry, revision_id="r1")
    after = at.geometry_identity(dataclasses.replace(geometry, sha256="d" * 64), revision_id="r1")
    assert before["revision_id"] == after["revision_id"] == "r1"
    assert before["sha256"] != after["sha256"]
    assert before != after


def test_same_revision_id_with_identical_bytes_is_accepted(geometry):
    assert at.geometry_identity(geometry, revision_id="r1") == \
           at.geometry_identity(geometry, revision_id="r1")


def test_mutation_geometry_relocation_with_identical_checksum_succeeds(geometry):
    payload = _approved(geometry)
    payload["geometry_source"] = dataclasses.replace(
        geometry, object_key="sources/relocated-elsewhere").to_payload()
    validate(payload, where="reconstruction")


def test_mutation_geometry_checksum_substitution_fails(geometry):
    payload = _approved(geometry)
    payload["geometry_source"] = dataclasses.replace(geometry, sha256="e" * 64).to_payload()
    _refused(payload)


def test_mutation_geometry_source_substitution_fails(geometry):
    payload = _approved(geometry)
    payload["geometry_source"] = dataclasses.replace(
        geometry, source_id="99999999-9999-4999-8999-999999999999").to_payload()
    _refused(payload)


def test_absent_geometry_fails_closed(geometry):
    empty = at.geometry_identity(None)
    assert empty["sha256"] == "" and empty["source_id"] == ""
    assert empty != at.geometry_identity(geometry)


def test_local_absolute_paths_never_enter_the_fingerprint(tmp_path, geometry):
    canon = at.approved_intent_canonical(
        engine="gmsh", purpose="internal_cfd", input_kind="fluid-domain", dimensionality="3D",
        patches=_PATCHES, engine_params={}, requested_mesh_fidelity="draft",
        request_txt=_REQUEST, source_ref=geometry)
    blob = repr(canon)
    assert str(tmp_path) not in blob
    assert geometry.object_key not in blob
    assert geometry.original_filename not in blob


# required outputs: one authority, engine-derived
def test_the_envelope_carries_all_four_fidelity_fields():
    canon = at.approved_intent_canonical(
        engine="gmsh", purpose="internal_cfd", input_kind="fluid-domain", dimensionality="3D",
        patches=[], engine_params={}, requested_mesh_fidelity="draft", request_txt="x",
        source_ref=None)
    for k in ("requested_mesh_fidelity", "effective_mesh_fidelity",
              "mesh_fidelity_source", "fidelity_policy_version"):
        assert k in canon, f"the approved-intent envelope must carry {k}"
    assert canon["mesh_fidelity_source"] == "user"
    assert canon["fidelity_policy_version"]
    # dropping any one must change the fingerprint (so a mutation that omits it is detected)
    full = at.fingerprint(canon)
    for k in ("mesh_fidelity_source", "fidelity_policy_version"):
        assert at.fingerprint({kk: vv for kk, vv in canon.items() if kk != k}) != full


def test_required_outputs_come_from_the_single_policy_and_are_version_pinned():
    from meshpipeline.application.artifact_policy import (
        ARTIFACT_POLICY_VERSION,
        required_output_classes,
    )
    canon = at.approved_intent_canonical(
        engine="gmsh", purpose="internal_cfd", input_kind="fluid-domain", dimensionality="3D",
        patches=[], engine_params={}, requested_mesh_fidelity=None, request_txt="x",
        source_ref=None)
    assert canon["required_outputs"] == required_output_classes("gmsh")
    assert canon["artifact_policy_version"] == ARTIFACT_POLICY_VERSION
    assert "required_outputs" not in accepted_keys()        # never payload-supplied
    assert "artifact_policy_version" not in accepted_keys()


def test_mutation_required_output_drift_via_engine_is_refused(geometry):
    _refused(_approved(geometry, mesh_engine="cfmesh"))


def test_unset_engine_derives_no_policy_and_is_not_ready():
    from meshpipeline.application.artifact_policy import required_output_classes, required_ready
    assert required_output_classes("") == [] and required_output_classes("no-such") == []
    assert required_ready("", ["mesh_bundle"]) is False     # fails closed, never vacuously true


# a direct/unapproved job stays explicitly distinguishable
def test_direct_job_without_a_snapshot_carries_no_fingerprint_and_is_allowed(geometry):
    p = _approved(geometry)
    p["approved_snapshot_id"] = ""
    p["approved_intent_fingerprint"] = ""
    validate(p, where="reconstruction")
