# Responsibility: Verify a delivered patch set matches the contract exactly, and the diagnostic says what to change.
from __future__ import annotations

from meshpipeline.engines.contract import check_contract


def test_empty_contract_is_always_satisfied():
    ok, diag = check_contract([], ["wall", "inlet"], {"wall": "wall", "inlet": "inlet"})
    assert ok is True
    assert diag == ""


def test_empty_contract_with_empty_manifest_also_skipped():
    ok, diag = check_contract([], [], {})
    assert ok is True
    assert diag == ""



_CONTRACT_AIRFOIL = [
    {"name": "airfoil",  "type": "wall"},
    {"name": "inlet",    "type": "inlet"},
    {"name": "outlet",   "type": "outlet"},
    {"name": "symmetry", "type": "symmetry"},
]


def test_exact_match_passes():
    ok, diag = check_contract(
        _CONTRACT_AIRFOIL,
        ["airfoil", "inlet", "outlet", "symmetry"],
        {"airfoil": "wall", "inlet": "inlet", "outlet": "outlet", "symmetry": "symmetry"},
    )
    assert ok is True
    assert diag == ""


def test_exact_match_passes_when_manifest_patches_is_a_dict():
    ok, diag = check_contract(
        _CONTRACT_AIRFOIL,
        {"airfoil": [1, 2], "inlet": [10], "outlet": [11], "symmetry": [12, 13, 14, 15]},
        {"airfoil": "wall", "inlet": "inlet", "outlet": "outlet", "symmetry": "symmetry"},
    )
    assert ok is True, f"dict-shaped manifest patches must pass when types match - got {diag[:200]}"
    assert diag == ""



def test_wall_renamed_is_caught():
    ok, diag = check_contract(
        _CONTRACT_AIRFOIL,
        ["wall", "inlet", "outlet", "symmetry"],
        {"wall": "wall", "inlet": "inlet", "outlet": "outlet", "symmetry": "symmetry"},
    )
    assert ok is False
    assert "[CONTRACT_MISMATCH]" in diag
    assert "airfoil" in diag
    assert "wall" in diag
    assert "MISSING" in diag
    assert "EXTRA" in diag


def test_symmetry_fragmented_into_four_is_caught():
    ok, diag = check_contract(
        _CONTRACT_AIRFOIL,
        ["airfoil", "inlet", "outlet", "top", "bottom", "front", "back"],
        {
            "airfoil": "wall", "inlet": "inlet", "outlet": "outlet",
            "top": "symmetry", "bottom": "symmetry",
            "front": "symmetry", "back": "symmetry",
        },
    )
    assert ok is False
    assert "[CONTRACT_MISMATCH]" in diag
    assert "symmetry" in diag
    for n in ("top", "bottom", "front", "back"):
        assert n in diag


def test_type_mismatch_caught():
    ok, diag = check_contract(
        [{"name": "airfoil", "type": "wall"}, {"name": "farfield", "type": "farfield"}],
        ["airfoil", "farfield"],
        {"airfoil": "symmetry", "farfield": "farfield"},
    )
    assert ok is False
    assert "TYPE MISMATCH" in diag
    assert "airfoil" in diag
    assert "wall" in diag
    assert "symmetry" in diag


def test_empty_manifest_against_contract():
    ok, diag = check_contract(_CONTRACT_AIRFOIL, [], {})
    assert ok is False
    assert "[CONTRACT_MISMATCH]" in diag


def test_diagnostic_starts_at_line_one_so_classifier_regex_matches():
    ok, diag = check_contract(
        _CONTRACT_AIRFOIL,
        ["wall"],
        {"wall": "wall"},
    )
    assert ok is False
    assert diag.lstrip().startswith("[CONTRACT_MISMATCH]"), (
        "Diagnostic must lead with the [CONTRACT_MISMATCH] tag so the "
        "classifier's deterministic regex picks it up."
    )



def test_diagnostic_tells_builder_what_to_change():
    ok, diag = check_contract(
        _CONTRACT_AIRFOIL,
        ["wall", "inlet", "outlet", "symmetry"],
        {"wall": "wall", "inlet": "inlet", "outlet": "outlet", "symmetry": "symmetry"},
    )
    assert ok is False
    assert "fix the patch definitions" in diag.lower()
    assert "do not change mesh sizing" in diag.lower() or "do NOT" in diag


_CONTRACT_2D_SEPARATE_FRONTBACK = [
    {"name": "airfoil_wall", "type": "wall"},
    {"name": "farfield",     "type": "farfield"},
    {"name": "outlet",       "type": "outlet"},
    {"name": "front",        "type": "empty"},
    {"name": "back",         "type": "empty"},
]


def test_2d_merged_frontandback_satisfies_separate_frontback_contract():
    ok, diag = check_contract(
        _CONTRACT_2D_SEPARATE_FRONTBACK,
        ["airfoil_wall", "farfield", "outlet", "frontAndBack"],
        {"airfoil_wall": "wall", "farfield": "farfield", "outlet": "outlet", "frontAndBack": "empty"},
    )
    assert ok is True, diag
    assert diag == ""


def test_2d_separate_frontback_also_satisfies_separate_contract():
    ok, diag = check_contract(
        _CONTRACT_2D_SEPARATE_FRONTBACK,
        ["airfoil_wall", "farfield", "outlet", "front", "back"],
        {"airfoil_wall": "wall", "farfield": "farfield", "outlet": "outlet", "front": "empty", "back": "empty"},
    )
    assert ok is True, diag


def test_empty_collapse_does_not_mask_missing_real_patch():
    ok, diag = check_contract(
        _CONTRACT_2D_SEPARATE_FRONTBACK,
        ["airfoil_wall", "outlet", "frontAndBack"],
        {"airfoil_wall": "wall", "outlet": "outlet", "frontAndBack": "empty"},
    )
    assert ok is False
    assert "farfield" in diag


def test_missing_empty_patch_still_rejected():
    ok, diag = check_contract(
        _CONTRACT_2D_SEPARATE_FRONTBACK,
        ["airfoil_wall", "farfield", "outlet"],
        {"airfoil_wall": "wall", "farfield": "farfield", "outlet": "outlet"},
    )
    assert ok is False
