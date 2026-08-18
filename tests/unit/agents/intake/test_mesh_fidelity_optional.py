# Responsibility: Verify mesh fidelity is optional in the schema, and an omitted tier is accepted rather than asked for.
from __future__ import annotations

import copy

import pytest

from meshpipeline.agents.intake.agent import INTAKE_TOOLS
from meshpipeline.agents.intake.validation import validate_submission

_BASE = {
    "domain": "duct", "request_txt": "r" * 120, "review_brief_txt": "b" * 90,
    "dimensionality": "3D", "purpose": "internal_cfd", "input_kind": "fluid-domain",
    "mesh_engine": "gmsh", "engine_source": "user_direct",
    "engine_params": {"element_order": "2"},
    "patches": [{"name": "inlet", "type": "inlet"}, {"name": "outlet", "type": "outlet"},
                {"name": "wall", "type": "wall"}],
}


def _submit_schema() -> dict:
    for t in INTAKE_TOOLS:
        if t["function"]["name"] == "submit_requirements":
            return t["function"]
    raise AssertionError("submit_requirements not found")


def _submit(**over):
    a = copy.deepcopy(_BASE)
    a.update(over)
    return a


# the schema makes it optional, three-tier, and never-ask
def test_mesh_fidelity_is_not_in_the_required_list():
    assert "mesh_fidelity" not in _submit_schema()["parameters"]["required"]


def test_mesh_fidelity_enum_is_exactly_three_tiers():
    enum = _submit_schema()["parameters"]["properties"]["mesh_fidelity"]["enum"]
    assert set(enum) == {"draft", "standard", "max"}


def test_schema_tells_the_model_not_to_ask_and_to_omit_when_unstated():
    desc = _submit_schema()["parameters"]["properties"]["mesh_fidelity"]["description"]
    low = desc.lower()
    assert "optional" in low
    assert "do not ask" in low or "never" in low
    assert "omit" in low
    assert "cell" in low                       # explicitly says not a cell count


# the validator accepts omission, rejects only a non-empty invalid value
@pytest.mark.parametrize("val", ["__omit__", None, "", "   ", "\t"])
def test_omitted_or_blank_fidelity_is_accepted(val):
    a = _submit()
    if val != "__omit__":
        a["mesh_fidelity"] = val
    assert not [e for e in validate_submission(a) if "fidelity" in e.lower()]


@pytest.mark.parametrize("val", ["draft", "standard", "max"])
def test_valid_values_are_accepted(val):
    assert not [e for e in validate_submission(_submit(mesh_fidelity=val)) if "fidelity" in e.lower()]


@pytest.mark.parametrize("val", ["ultra", "fine", "low", "1000000"])
def test_a_non_empty_invalid_tier_is_rejected(val):
    errs = [e for e in validate_submission(_submit(mesh_fidelity=val)) if "fidelity" in e.lower()]
    assert errs, f"{val!r} should be rejected"


def test_a_valid_submission_with_no_fidelity_has_no_errors_at_all():
    assert validate_submission(_submit()) == []


def test_no_exact_cell_count_field_is_required():
    req = _submit_schema()["parameters"]["required"]
    for banned in ("target_cells", "max_cells", "cell_budget", "max_cell_budget", "element_count"):
        assert banned not in req
    props = _submit_schema()["parameters"]["properties"]
    assert not any("cell" in k for k in props)


# the node resolves the tier ONLY from the explicit tool field, never from prose
def test_intake_resolves_fidelity_only_from_the_tool_field_not_prose():
    import ast
    import inspect

    # fidelity is resolved once by validation.normalise_submission, from the TOOL field only
    from meshpipeline.agents.intake import validation as _v
    src = inspect.getsource(_v.normalise_submission)
    tree = ast.parse(src)
    # find the resolve_mesh_fidelity(...) call in the submit block
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") in ("_resolve_fid", "resolve_mesh_fidelity")]
    assert calls, "the intake node must resolve the tier via the shared policy"
    for c in calls:
        arg_src = ast.get_source_segment(src, c.args[0])
        assert "request_txt" not in arg_src, (
            f"fidelity must not be inferred from prose - resolver arg was: {arg_src}")
        assert "mesh_fidelity" in arg_src      # it comes from the explicit tool field
