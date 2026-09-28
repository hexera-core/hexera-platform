# Responsibility: Verify every patch name a person types becomes one spelling a mesher can write, and that every layer uses it.
# Boundaries: the name rule, the intake's tool boundary and the engine admission; engines' own writers are not exercised here.
from __future__ import annotations

import asyncio

import pytest

from meshpipeline.contracts.patch_names import (
    is_mesh_safe,
    mesh_safe,
    normalize_patches,
    rename_note,
)

# the rule

@pytest.mark.parametrize("typed,safe", [
    ("car wall", "car_wall"),                 # job ac1daa3e: a space, 26 minutes, "zero faces"
    ("Inlet-1", "Inlet_1"),
    ("  outlet  ", "outlet"),
    ("2nd outlet", "p_2nd_outlet"),           # a leading digit
    ("wall (car body)", "wall_car_body"),
    ("Einlass/Äußere Wand", "Einlass_u_ere_Wand"),
    ("car__wall", "car__wall"),               # already safe: untouched
    ("farfield", "farfield"),
    ("---", "patch"),                         # nothing usable
    ("_hidden", "hidden"),
])
def test_a_typed_name_becomes_one_a_mesher_can_write(typed, safe):
    assert mesh_safe(typed) == safe
    assert is_mesh_safe(mesh_safe(typed))


def test_a_long_name_is_cut_to_a_safe_length():
    name = mesh_safe("a very long boundary name " * 10)
    assert is_mesh_safe(name) and len(name) <= 64


def test_the_car_body_patches_from_dev_come_out_mesh_safe():
    # The exact list the intake approved on shared dev: car wall / ground / farfield.
    typed = [{"name": "car wall", "type": "wall"}, {"name": "ground", "type": "wall"},
             {"name": "farfield", "type": "farfield"}]
    out, renames = normalize_patches(typed)
    assert [p["name"] for p in out] == ["car_wall", "ground", "farfield"]
    assert [p["type"] for p in out] == ["wall", "wall", "farfield"]
    assert renames == {"car wall": "car_wall"}
    assert "car_wall" in rename_note(renames) and "retype" in rename_note(renames)


def test_two_names_that_meet_are_told_apart():
    out, renames = normalize_patches([{"name": "car wall"}, {"name": "car-wall"},
                                      {"name": "car_wall"}])
    names = [p["name"] for p in out]
    assert len(set(names)) == 3 and all(is_mesh_safe(n) for n in names)


def test_references_follow_their_renames():
    out, _ = normalize_patches([
        {"name": "inlet 1", "type": "inlet", "interchangeable_with": ["inlet 2"]},
        {"name": "inlet 2", "type": "inlet", "interchangeable_with": ["inlet 1"]}])
    assert out[0]["interchangeable_with"] == ["inlet_2"]
    assert out[1]["interchangeable_with"] == ["inlet_1"]


def test_safe_names_come_back_unchanged_and_nothing_is_reported():
    typed = [{"name": "inlet", "type": "inlet"}, {"name": "wall", "type": "wall"}]
    out, renames = normalize_patches(typed)
    assert out == typed and renames == {} and rename_note(renames) == ""


def test_malformed_input_is_left_for_the_validators():
    assert normalize_patches(None) == (None, {})
    assert normalize_patches("inlet") == ("inlet", {})
    out, renames = normalize_patches([{"type": "wall"}, "x", {"name": "a b"}])
    assert out[0] == {"type": "wall"} and out[1] == "x" and out[2]["name"] == "a_b"


# the engine admission: the last gate before a mesh runs

def test_every_engine_refuses_an_unsafe_name_before_meshing():
    from meshpipeline.engines.admission import AdmissionEvidence, PatchSummary
    from meshpipeline.engines.registry import engine_names, get_spec

    for name in engine_names():
        spec = get_spec(name)
        ev = AdmissionEvidence(engine=name, purpose="external_cfd", input_kind="body-surface",
                               dimensionality="3D",
                               patches=(PatchSummary(name="car wall", type="wall"),
                                        PatchSummary(name="farfield", type="farfield")))
        codes = {r.code for r in spec.admit(ev)}
        assert "patch_name_unsafe" in codes, spec.name
        rej = next(r for r in spec.admit(ev) if r.code == "patch_name_unsafe")
        assert rej.expected == "car_wall" and "car_wall" in rej.message


def test_a_safe_name_is_never_refused_for_its_spelling():
    from meshpipeline.engines.admission import AdmissionEvidence, PatchSummary
    from meshpipeline.engines.registry import engine_names, get_spec

    for name in engine_names():
        ev = AdmissionEvidence(engine=name, purpose="external_cfd", input_kind="body-surface",
                               dimensionality="3D",
                               patches=(PatchSummary(name="car_wall", type="wall"),
                                        PatchSummary(name="farfield", type="farfield")))
        assert "patch_name_unsafe" not in {r.code for r in get_spec(name).admit(ev)}, name


# the intake's tool boundary

def test_the_intake_hands_every_handler_the_mesh_safe_names_and_tells_the_model():
    from meshpipeline.agents.intake.executor import (
        IntakeExecutionState,
        IntakeToolExecutor,
        IntakeToolResult,
    )

    seen: dict = {}
    ex = IntakeToolExecutor(state=IntakeExecutionState(session_id="s", owner_id="o"),
                            job_id="j", implemented_engines=["snappy"], search_tool=None,
                            trace=None)

    async def _fake(args):
        seen.update(args)
        return IntakeToolResult(tool="preview_selected_admission", accepted=True, content="ok")

    ex._do_preview_selected_admission = _fake  # type: ignore[method-assign]
    res = asyncio.run(ex.run("preview_selected_admission", {
        "selected_engine": "snappy", "purpose": "external_cfd",
        "patches": [{"name": "car wall", "type": "wall"}, {"name": "ground", "type": "wall"},
                    {"name": "farfield", "type": "farfield"}]}))
    assert [p["name"] for p in seen["patches"]] == ["car_wall", "ground", "farfield"]
    assert "car_wall" in res.content and res.content.endswith("ok")
