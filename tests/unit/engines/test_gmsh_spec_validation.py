# Responsibility: Verify the Gmsh spec validator rejects an unknown key or out-of-range value rather than ignoring it.
from __future__ import annotations

from meshpipeline.engines.gmsh.driver import _validate_spec  # noqa: E402

_VALID = {
    "element_order": 2,
    "size": {"mode": "factor", "value": 0.04},
    "groups": [{"name": "fixed_base", "role": "fixed", "surface_tags": [3, 4]}],
    "default_group": "free",
    "optimize": True,
}


def test_a_well_formed_spec_passes():
    assert _validate_spec(_VALID) == []


def test_unknown_top_level_key_is_rejected_not_ignored():
    # a misspelling (element_ordr) or an unsupported capability must FAIL, not
    # silently fall back to the default order
    errs = _validate_spec({**_VALID, "element_ordr": 1})
    assert any("unknown key" in e and "element_ordr" in e for e in errs), errs


def test_out_of_range_values_are_rejected():
    assert any("element_order must be 1 or 2" in e
               for e in _validate_spec({**_VALID, "element_order": 3}))
    assert any("size.mode" in e
               for e in _validate_spec({**_VALID, "size": {"mode": "relative", "value": 0.1}}))
    assert any("role must be one of" in e
               for e in _validate_spec({**_VALID,
                   "groups": [{"name": "g", "role": "clamped", "surface_tags": [1]}]}))


def test_unknown_nested_field_and_unsupported_export_are_rejected():
    assert any("size has unknown field" in e
               for e in _validate_spec({**_VALID, "size": {"mode": "factor", "value": 0.04, "grading": 1.2}}))
    assert any("extra_exports has unsupported" in e
               for e in _validate_spec({**_VALID, "extra_exports": ["bdf", "cgns"]}))


def test_defaults_apply_only_to_recognized_omitted_keys():
    # a MINIMAL valid spec (omitting optional keys) is accepted - defaults fill them
    # AFTER validation; only unknown keys are rejected.
    assert _validate_spec({"groups": [{"name": "g", "role": "fixed", "surface_tags": [1]}]}) == []
