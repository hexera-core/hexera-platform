# Responsibility: Verify fidelity resolution records provenance, refuses an invalid tier, and tells an edit from drift.
from __future__ import annotations

import hashlib
import unicodedata

import pytest

import meshpipeline.agents.intake.admission_token as at
from meshpipeline.pipeline.enums import (
    FIDELITY_POLICY_VERSION,
    FidelityContractError,
    MeshFidelity,
    MeshFidelitySource,
    assert_fidelity_consistent,
    canonical_mesh_fidelity,
    render_fidelity_line,
    resolve_mesh_fidelity,
)


# the resolver
@pytest.mark.parametrize("v", [None, "", "   "])
def test_unstated_resolves_to_default_provenance(v):
    req, eff, src = resolve_mesh_fidelity(v)
    assert req is None and eff is MeshFidelity.STANDARD and src is MeshFidelitySource.DEFAULT


@pytest.mark.parametrize("v", ["draft", "standard", "max"])
def test_stated_resolves_to_user_provenance(v):
    req, eff, src = resolve_mesh_fidelity(v)
    assert req.value == eff.value == v and src is MeshFidelitySource.USER




def test_invalid_value_raises():
    with pytest.raises(FidelityContractError, match="not one of"):
        canonical_mesh_fidelity("ultra")


@pytest.mark.parametrize("v", ["high", "High", " HIGH "])
def test_high_is_not_an_accepted_tier(v):
    with pytest.raises(FidelityContractError):
        canonical_mesh_fidelity(v)


def test_empty_string_is_never_a_second_null():
    assert canonical_mesh_fidelity("") is None and canonical_mesh_fidelity(None) is None


# the consistency guard
def test_consistent_trios_pass():
    assert_fidelity_consistent(requested="draft", effective="draft", source="user",
                               policy_version=FIDELITY_POLICY_VERSION)
    assert_fidelity_consistent(requested=None, effective="standard", source="default",
                               policy_version=FIDELITY_POLICY_VERSION)


@pytest.mark.parametrize("kw", [
    {"requested": None, "effective": "standard", "source": "user"},
    {"requested": "draft", "effective": "draft", "source": "default"},
    {"requested": "draft", "effective": "max", "source": "user"},
    {"requested": None, "effective": "draft", "source": "default"},
    {"requested": "draft", "effective": "ultra", "source": "user"},
    {"requested": "draft", "effective": "draft", "source": "system"},
])
def test_inconsistent_trios_raise(kw):
    with pytest.raises(FidelityContractError):
        assert_fidelity_consistent(policy_version=FIDELITY_POLICY_VERSION, **kw)


def test_policy_version_mismatch_raises():
    with pytest.raises(FidelityContractError, match="policy"):
        assert_fidelity_consistent(requested="draft", effective="draft", source="user",
                                   policy_version="3tier-v2")


# rendering (approval summary + terminal), provenance-aware and legacy-aware
def test_render_distinguishes_user_from_default():
    assert render_fidelity_line(effective="standard", source="default") == \
        "Mesh detail preference: Standard (system default)"
    assert render_fidelity_line(effective="max", source="user") == \
        "Mesh detail preference: Max (user requested)"




def test_render_is_empty_when_it_would_mislead():
    assert render_fidelity_line(effective="", source="") == ""


# the approved-request digest
_BASE = "Mesh the duct. Inlet patch INLET_A. Gap 0.01 mm. High priority."


def _same(a, b):
    return at.approved_request_digest(a) == at.approved_request_digest(b)


def test_cosmetic_edits_are_not_drift():
    assert _same(_BASE, "  Mesh the   duct.  Inlet patch INLET_A. Gap 0.01 mm. High priority. ")
    assert _same(_BASE, _BASE.replace(". ", ".\r\n"))          # CRLF
    assert _same(_BASE, _BASE.replace(". ", ".\r"))            # CR
    # NFC vs NFD of the same text
    nfc = "Mesh the café duct"
    assert _same(nfc, unicodedata.normalize("NFD", nfc))


def test_unicode_normalization_is_nfc_and_explicit():
    nfc = "Mesh the café duct"
    d = at.approved_request_digest(unicodedata.normalize("NFD", nfc))
    expect = hashlib.sha256(
        " ".join(unicodedata.normalize("NFC", nfc).split()).encode("utf-8")).hexdigest()
    assert d == expect


def test_substantive_edits_are_drift():
    for changed in (
        _BASE.replace("0.01 mm", "0.01 m"),      # units
        _BASE.replace("0.01", "0.02"),           # number
        _BASE.replace("INLET_A", "inlet_a"),     # identifier case (NOT folded - semantic)
        _BASE.replace(" High priority.", ""),    # requirement removed
        _BASE + " Also refine the outlet.",      # requirement added
        "High priority. " + _BASE,               # reordered
    ):
        assert not _same(_BASE, changed), f"should be drift: {changed!r}"


def test_case_is_not_folded_wholesale():
    assert not _same("Inlet", "inlet")


def test_prose_case_differs_in_the_digest():
    assert not _same("Use High detail", "Use high detail")


def test_the_digest_never_carries_a_fidelity_tier():
    canon = at.approved_intent_canonical(
        engine="gmsh", purpose="internal_cfd", input_kind="fluid-domain", dimensionality="3D",
        patches=[], engine_params={}, requested_mesh_fidelity="max",
        request_txt="make it maximum detail", source_ref=None)
    assert canon["approved_request_digest"] not in {"draft", "standard", "max"}
    assert canon["effective_mesh_fidelity"] == "max"     # the tier lives in its own typed field


# legacy compatibility




def test_a_v3_record_verifies_and_reports_its_effective_tier():
    v3 = at.approved_intent_canonical(
        engine="gmsh", purpose="internal_cfd", input_kind="fluid-domain", dimensionality="3D",
        patches=[], engine_params={}, requested_mesh_fidelity=None, request_txt="x",
        source_ref=None)
    assert at.verify_approved_intent(v3, at.fingerprint(v3)) is True
    assert at.execution_fidelity_for(v3) == "standard"       # defaulted


def test_verify_rejects_an_unknown_schema_version():
    assert at.verify_approved_intent({"schema_version": 99}, "x") is False


# the approval summary shows the operational mesh-detail decision
def test_the_brief_shows_the_effective_tier_with_provenance():
    from types import SimpleNamespace

    from meshpipeline.application.intake_brief import build_brief

    def _brief(requested):
        return build_brief(SimpleNamespace(
            request_txt="x", purpose="internal_cfd", mesh_engine="gmsh",
            input_kind="fluid-domain", dimensionality="3D", domain="d",
            requested_mesh_fidelity=requested, intake_patches=[{"name": "inlet", "role": "inlet"}],
            engine_params=None, review_brief_txt="y", intake_submitted=True))

    assert _brief("draft")["mesh_fidelity_label"] == "Draft (user requested)"
    assert _brief(None)["mesh_fidelity_label"] == "Standard (system default)"
    # never High for a new approval
    assert "High" not in _brief("draft")["mesh_fidelity_label"]
    assert "High" not in _brief(None)["mesh_fidelity_label"]
