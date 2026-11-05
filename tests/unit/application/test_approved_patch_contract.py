# Responsibility: Verify the approved patch contract is satisfied only by the exact set, in any order, verified on read.
from __future__ import annotations

import pytest

from meshpipeline.application.approved_patch_contract import (
    ApprovedPatchContract as C,
)
from meshpipeline.application.approved_patch_contract import (
    PatchContractError,
    compute_fingerprint,
    normalize_patches,
)

_P = [{"name": "inlet", "type": "inlet"},
      {"name": "outlet", "type": "outlet"},
      {"name": "wall", "type": "wall"}]


def test_exact_set_is_satisfied_in_any_order():
    c = C.build(_P)
    c.assert_satisfied_by(list(reversed(_P)))              # order is not identity
    assert c.matches(_P) and c.matches(list(reversed(_P)))


def test_a_renamed_patch_with_the_same_count_is_rejected():
    c = C.build(_P)
    renamed = [{"name": "INLET", "type": "inlet"}, *_P[1:]]
    assert not c.matches(renamed)
    with pytest.raises(PatchContractError, match="does not match"):
        c.assert_satisfied_by(renamed)


def test_a_role_change_with_the_same_names_is_rejected():
    c = C.build(_P)
    re_roled = [{"name": "inlet", "type": "wall"}, *_P[1:]]
    assert not c.matches(re_roled)
    with pytest.raises(PatchContractError, match="role_changed"):
        c.assert_satisfied_by(re_roled)


def test_one_missing_and_one_extra_are_both_rejected():
    c = C.build(_P)
    with pytest.raises(PatchContractError, match="missing"):
        c.assert_satisfied_by(_P[:2])
    with pytest.raises(PatchContractError, match="added"):
        c.assert_satisfied_by(_P + [{"name": "z", "type": "wall"}])


def test_a_duplicate_patch_is_rejected_by_normalisation():
    with pytest.raises(PatchContractError, match="duplicate"):
        normalize_patches(_P + [{"name": "wall", "type": "wall"}])


def test_a_malformed_entry_is_rejected():
    for bad in (["not-a-dict"], [{"role": "wall"}], [{"name": "  ", "type": "wall"}]):
        with pytest.raises(PatchContractError):
            normalize_patches(bad)


def test_the_fingerprint_is_verified_on_read():
    d = C.build(_P).to_dict()
    d["fingerprint"] = "0" * 64
    with pytest.raises(PatchContractError, match="fingerprint"):
        C.from_dict(d)
    # patches tampered, old fingerprint retained → also fails
    d2 = C.build(_P).to_dict()
    d2["patches"] = d2["patches"][:2]
    with pytest.raises(PatchContractError, match="fingerprint"):
        C.from_dict(d2)


def test_the_fingerprint_changes_with_any_identity_change_but_not_with_order():
    base = compute_fingerprint(True, normalize_patches(_P))
    assert base == compute_fingerprint(True, normalize_patches(list(reversed(_P))))  # order-free
    assert base != compute_fingerprint(True, normalize_patches(_P[:2]))              # drop
    assert base != compute_fingerprint(True, normalize_patches(
        [{"name": "inlet", "type": "wall"}, *_P[1:]]))                                # re-role
    assert base != compute_fingerprint(False, normalize_patches(_P))                 # required flips


def test_an_unknown_schema_version_fails_safe():
    d = C.build(_P).to_dict()
    d["schema_version"] = 999
    with pytest.raises(PatchContractError, match="not supported"):
        C.from_dict(d)


def test_the_contract_is_typed_not_a_bare_boolean():
    c = C.build(_P)
    assert c.required is True
    assert c.patches == (("inlet", "inlet"), ("outlet", "outlet"), ("wall", "wall"))
    assert len(c.fingerprint) == 64
    # required=False is only satisfied by an EMPTY execution set - never by "any patches"
    empty = C.build([], required=False)
    empty.assert_satisfied_by([])
    with pytest.raises(PatchContractError, match="does not match"):
        empty.assert_satisfied_by(_P)


def test_required_true_with_no_patches_is_incoherent():
    with pytest.raises(PatchContractError):
        C.build([], required=True)
