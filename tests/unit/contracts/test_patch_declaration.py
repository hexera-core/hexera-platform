# Responsibility: Verify a declared patch list becomes one every layer reads the same way - roles people say, names the meshers keep, names only capitals tell apart, the ground under a body - and that every change is told to the model.
# Boundaries: the normaliser, the intake validator's backstops and the executor's tool boundary; engine admission and case writers have their own suites.
from __future__ import annotations

import asyncio

import pytest

from meshpipeline.contracts.patch_names import (
    GroundRule,
    canonical_role,
    declaration_note,
    is_reserved,
    normalize_declaration,
    role_hint,
)
from meshpipeline.engines.purposes import PURPOSES

EXTERNAL = PURPOSES["external_cfd"].boundary_roles
INTERNAL = PURPOSES["internal_cfd"].boundary_roles
STRUCTURAL = PURPOSES["structural"].boundary_roles


def _norm(patches, roles=EXTERNAL, ground=True):
    return normalize_declaration(patches, allowed_roles=roles,
                                 ground=GroundRule() if ground else None)


# the words people use for a boundary

@pytest.mark.parametrize("word,roles,role", [
    ("velocity inlet", INTERNAL, "inlet"), ("Velocity-Inlet", INTERNAL, "inlet"),
    ("mass flow inlet", INTERNAL, "inlet"), ("pressure outlet", INTERNAL, "outlet"),
    ("no-slip wall", INTERNAL, "wall"), ("No Slip", EXTERNAL, "wall"),
    ("far field", EXTERNAL, "farfield"), ("Far-Field", EXTERNAL, "farfield"),
    ("outer boundary", EXTERNAL, "farfield"), ("freestream", EXTERNAL, "farfield"),
    ("opening", EXTERNAL, "farfield"), ("symmetry plane", EXTERNAL, "symmetry"),
    ("symmetryPlane", EXTERNAL, "symmetry"), ("front and back", EXTERNAL, "empty"),
    ("fixed support", STRUCTURAL, "fixed"), ("encastre", STRUCTURAL, "fixed"),
    ("pressure", STRUCTURAL, "load"), ("force", STRUCTURAL, "load"),
    ("ground plane", EXTERNAL, "wall"), ("road", EXTERNAL, "wall"),
])
def test_a_word_with_one_meaning_here_becomes_that_role(word, roles, role):
    assert canonical_role(word, roles) == role


@pytest.mark.parametrize("word,roles", [("opening", INTERNAL), ("pressure", INTERNAL),
                                         ("support", EXTERNAL), ("banana", None)])
def test_a_word_with_no_single_meaning_here_is_left_for_a_question(word, roles):
    assert canonical_role(word, roles) is None


def test_the_question_names_the_choice():
    hint = role_hint("opening", INTERNAL)
    assert "'inlet'" in hint and "'outlet'" in hint and "ask the user" in hint
    assert role_hint("banana", INTERNAL) == ""


def test_types_are_read_and_each_change_is_told():
    out = _norm([{"name": "duct", "type": "no-slip wall"},
                 {"name": "feed", "type": "velocity inlet", "diameter_mm": 40},
                 {"name": "drain", "type": "pressure outlet", "diameter_mm": 60}],
                roles=INTERNAL, ground=False)
    assert [p["type"] for p in out.patches] == ["wall", "inlet", "outlet"]
    assert out.patches[1]["diameter_mm"] == 40                    # nothing else is touched
    note = declaration_note(out)
    for typed, role in (("no-slip wall", "wall"), ("velocity inlet", "inlet"),
                        ("pressure outlet", "outlet")):
        assert f"typed '{typed}' is a {role}" in note


def test_a_change_of_capitals_is_fixed_without_a_sentence():
    out = _norm([{"name": "body", "type": "Wall"}, {"name": "farfield", "type": "FARFIELD"}])
    assert [p["type"] for p in out.patches] == ["wall", "farfield"]
    assert out.retypes == ()


def test_a_role_sent_instead_of_a_type_becomes_the_type():
    out = _norm([{"name": "body", "role": "wall"}, {"name": "ff", "type": "farfield",
                                                    "role": "farfield"}])
    assert out.patches == [{"name": "body", "type": "wall"}, {"name": "ff", "type": "farfield"}]


def test_a_role_that_contradicts_the_type_is_left_for_the_validator():
    out = _norm([{"name": "x", "type": "wall", "role": "inlet"}])
    assert out.patches[0]["role"] == "inlet" and out.patches[0]["type"] == "wall"


# names the meshers keep for themselves

@pytest.mark.parametrize("name", ["outer", "Outer", "FoamFile", "defaultFaces", "fixedWalls",
                                  "solid", "seedZone", "thinZone3", "topEmptyFaces"])
def test_a_name_a_mesher_keeps_is_the_users_own_after_the_boundary(name):
    assert is_reserved(name)
    out = _norm([{"name": name, "type": "wall"}, {"name": "farfield", "type": "farfield"}],
                ground=False)
    assert out.patches[0]["name"] == f"{name}_patch"
    assert not is_reserved(out.patches[0]["name"])
    assert "uses that name for a boundary of its own" in declaration_note(out)


def test_farfield_is_not_reserved_it_is_the_far_fields_own_name():
    assert not is_reserved("farfield")


# the same name twice, and different names that meet

def test_different_names_that_meet_after_cleaning_are_told_apart():
    out = _norm([{"name": "car wall", "type": "wall"}, {"name": "car-wall", "type": "wall"}],
                ground=False)
    assert [p["name"] for p in out.patches] == ["car_wall", "car_wall_2"]


@pytest.mark.parametrize("a,b", [("ground", "ground"), ("car wall", "car wall"),
                                 ("Inlet", "inlet"), ("ground", "Ground")])
def test_the_same_name_twice_stays_a_duplicate_for_the_validator_to_refuse(a, b):
    # a number would turn one boundary declared twice into two different boundaries - "ground"
    # twice into a floor and a new body wall - where the user needs to be asked
    out = _norm([{"name": a, "type": "wall"}, {"name": b, "type": "wall"},
                 {"name": "farfield", "type": "farfield"}])
    first, second = (p["name"] for p in out.patches[:2])
    assert first.casefold() == second.casefold()
    assert _errs(out.patches), "the duplicate reached admission unrefused"


def test_a_correctly_typed_name_is_never_the_one_that_moves():
    out = _norm([{"name": "car wall", "type": "wall"}, {"name": "car_wall", "type": "wall"}],
                ground=False)
    assert [p["name"] for p in out.patches] == ["car_wall_2", "car_wall"]


# the ground under a body in an external flow

@pytest.mark.parametrize("name,typ", [("ground plane", "wall"), ("floor", "wall"),
                                      ("road", "wall"), ("Road Surface", "wall"),
                                      ("ground_plane", "wall"), ("tarmac", "ground plane"),
                                      ("underside", "floor")])
def test_a_ground_by_any_word_becomes_the_ground_the_domain_lays(name, typ):
    out = _norm([{"name": "car", "type": "wall"}, {"name": name, "type": typ},
                 {"name": "farfield", "type": "farfield"}])
    assert out.patches[1] == {"name": "ground", "type": "wall"}
    assert "floor of the domain" in declaration_note(out)


def test_an_existing_ground_is_not_duplicated():
    out = _norm([{"name": "car", "type": "wall"}, {"name": "ground", "type": "wall"},
                 {"name": "floor", "type": "wall"}, {"name": "farfield", "type": "farfield"}])
    assert [p["name"] for p in out.patches] == ["car", "ground", "floor", "farfield"]


def test_inside_a_duct_a_floor_is_just_a_wall():
    out = _norm([{"name": "floor", "type": "wall"}], roles=INTERNAL, ground=False)
    assert out.patches[0]["name"] == "floor"


def test_the_normaliser_is_a_fixed_point():
    # the preview token binds the normalised preview; the submission is normalised again - so a
    # second pass must change nothing, or no submission could ever match its preview
    typed = [{"name": "car wall", "type": "no-slip wall"}, {"name": "Road Surface", "type": "wall"},
             {"name": "outer", "type": "far field"}, {"name": "OUTER", "type": "symmetry plane"},
             {"name": "2nd wing", "type": "WALL"}]
    once = _norm(typed).patches
    twice = _norm(once)
    assert twice.patches == once and not twice.changed


# the validator's backstops, for a caller that skipped the boundary

def _errs(patches, purpose="external_cfd", **over) -> list[str]:
    from meshpipeline.agents.intake.validation import validate_submission
    base = {"domain": "x", "request_txt": "A complete requirements summary of the case. " * 3,
            "review_brief_txt": "Acceptance criteria for the reviewer, in full. " * 3,
            "mesh_engine": "snappy", "engine_source": "user_direct", "purpose": purpose,
            "input_kind": "body-surface", "dimensionality": "3D", "engine_params": {},
            "flow_axis": "+x", "patches": patches}
    base.update(over)
    return validate_submission(base)


def test_a_reserved_name_is_refused_in_every_flow_with_the_fix_named():
    errs = _errs([{"name": "body", "type": "wall"}, {"name": "outer", "type": "farfield"}])
    assert any("'outer'" in e and "reserved" in e and "'outer_patch'" in e for e in errs), errs


def test_names_only_capitals_tell_apart_are_refused():
    errs = _errs([{"name": "Body", "type": "wall"}, {"name": "body", "type": "wall"},
                  {"name": "farfield", "type": "farfield"}])
    assert any("only in capitals" in e for e in errs), errs


def test_a_role_and_type_that_disagree_are_refused():
    errs = _errs([{"name": "body", "type": "wall", "role": "inlet"},
                  {"name": "farfield", "type": "farfield"}])
    assert any("one kind" in e for e in errs), errs


def test_a_floor_beside_the_ground_is_refused_as_a_second_floor():
    errs = _errs([{"name": "car", "type": "wall"}, {"name": "ground", "type": "wall"},
                  {"name": "floor", "type": "wall"}, {"name": "farfield", "type": "farfield"}])
    assert any("describe the ground" in e for e in errs), errs


def test_a_lone_floor_is_refused_with_the_name_that_works():
    errs = _errs([{"name": "car", "type": "wall"}, {"name": "floor", "type": "wall"},
                  {"name": "farfield", "type": "farfield"}])
    assert any("name it 'ground'" in e for e in errs), errs


def test_an_opening_in_a_duct_asks_which_it_is():
    errs = _errs([{"name": "wall", "type": "wall"},
                  {"name": "a", "type": "inlet", "diameter_mm": 40},
                  {"name": "b", "type": "opening", "diameter_mm": 100}],
                 purpose="internal_cfd", input_kind="solid-body")
    assert any("'opening'" in e and "'inlet' or 'outlet'" in e for e in errs), errs


# the intake's tool boundary

def _through(tool: str, args: dict):
    from meshpipeline.agents.intake.executor import (
        IntakeExecutionState,
        IntakeToolExecutor,
        IntakeToolResult,
    )
    seen: dict = {}
    ex = IntakeToolExecutor(state=IntakeExecutionState(session_id="s", owner_id="o"),
                            job_id="j", implemented_engines=["snappy"], search_tool=None,
                            trace=None)

    async def _fake(a):
        seen.update(a)
        return IntakeToolResult(tool=tool, accepted=True, content="ok")

    setattr(ex, f"_do_{tool}", _fake)
    res = asyncio.run(ex.run(tool, args))
    return seen, res.content


def test_the_boundary_reads_types_by_the_declared_purpose_and_tells_the_model():
    seen, content = _through("preview_selected_admission", {
        "selected_engine": "snappy", "purpose": "external_cfd",
        "patches": [{"name": "car wall", "type": "no-slip wall"},
                    {"name": "ground plane", "type": "wall"},
                    {"name": "far field", "type": "opening"}]})
    assert seen["patches"] == [{"name": "car_wall", "type": "wall"},
                               {"name": "ground", "type": "wall"},
                               {"name": "far_field", "type": "farfield"}]
    for said in ("'car wall' is now car_wall", "'ground plane' is now ground",
                 "typed 'opening' is a farfield", "typed 'no-slip wall' is a wall"):
        assert said in content, content
    assert content.endswith("ok")


def test_the_boundary_leaves_an_ambiguous_word_for_the_validator_to_ask():
    seen, _content = _through("submit_requirements", {
        "purpose": "internal_cfd",
        "patches": [{"name": "wall", "type": "wall"}, {"name": "port", "type": "opening"}]})
    assert seen["patches"][1]["type"] == "opening"
