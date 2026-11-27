# Responsibility: the geometry card does not offer an inlet or an outlet on a mouth the measuring
# step has read as having the part itself straight ahead of it, and the picture-naming step cannot
# hand that role back.
# Boundaries: pure functions only - vet_by_reach, _facts_text, _SYSTEM, _merge. No CAD, no
# rendering, no model, no database.
"""A PORT CUT ON A CAPPED MOUTH COMES BACK WITH NO FACES, and the card was offering one.

ColdPlateAssembly_CLEAN.step has three mouths. Two 8 mm hose mouths reach the outside; the 11.8 mm
lid port opens into 0.500 mm of flat metal under a tube it never joins, so no coolant ever gets
there. `cad/fluid_volume._port` measures that with a ray bounded at the assembly's own box and
writes `clear_ahead: false`; `cad/fluid_volume_check` scores that opening 0.5 for it.

WHAT IT COST, both at the END of a mesh, both refused for a boundary the region does not carry:

    job 6ddb58aa   the ENGINE named the blind mouth `coolant_in`   283,410,955 cells, 11,217.3 s
    job e8256476   the USER declared it `outlet_2` at intake        14,910.8 s, a second 64-core box

`engines/cfmesh_conjugate/mouths.sealed_rows` now refuses such a declaration before the mesher.
This file is about the layer above it: not asking the customer to confirm a port that is going to
be refused.

THE CARD THAT PROVOKED IT - session dff9eb3b, 2026-11-16, read off `GET /geometry/<sid>/check`:

    id 1  inlet     conf 0.99   clear_ahead true    reaches_outside true     8.0 mm
    id 2  outlet_1  conf 0.98   clear_ahead true    reaches_outside true     8.0 mm
    id 3  outlet_2  conf 0.98   clear_ahead false   reaches_outside false   11.8 mm

Sticker 3 is offered as a port at 0.98 on the same card that measures it blind. The 0.98 is the
picture-naming step's: the scout had scored it 0.5, and `_proposal` took the model's number for a
question a render cannot answer - a capped bore and an open one are the same dark circle.
"""
from __future__ import annotations

import pytest

from meshpipeline.application import geometry_check as gc

#: The three mouths of ColdPlateAssembly_CLEAN.step, as session dff9eb3b's card carries them.
#: Areas are pi/4 d^2 of the diameters on that card; the roles and confidences are verbatim.
PLATE = [
    {"id": 1, "name": "inlet", "role": "inlet", "shape": "circle", "diameter_mm": 8.0,
     "area_mm2": 50.27, "centroid_mm": [99.88, 54.68, 43.31], "normal": [0.0, 1.0, 0.0],
     "confidence": 0.99, "clear_ahead": True, "reaches_outside": True},
    {"id": 2, "name": "outlet_1", "role": "outlet", "shape": "circle", "diameter_mm": 8.0,
     "area_mm2": 50.27, "centroid_mm": [89.34, 54.68, 77.39], "normal": [0.0, 1.0, 0.0],
     "confidence": 0.98, "clear_ahead": True, "reaches_outside": True},
    {"id": 3, "name": "outlet_2", "role": "outlet", "shape": "circle", "diameter_mm": 11.8,
     "area_mm2": 109.36, "centroid_mm": [65.92, 33.45, 75.41], "normal": [0.0, 1.0, 0.0],
     "confidence": 0.98, "clear_ahead": False, "reaches_outside": False},
]


def _facts(openings=None, **over) -> dict:
    facts = {"body_kind": "single_solid", "input_kind": "solid-assembly", "flow": "internal",
             "openings_from": "fluid_volume", "solids": 6, "read_as": "cad",
             "size_mm": [177.5, 41.84, 101.0], "confidence": {"input_kind": 0.85}, "notes": [],
             "openings": [dict(o) for o in (PLATE if openings is None else openings)]}
    facts.update(over)
    return facts


def _roles(facts: dict) -> dict:
    return {o["id"]: o["role"] for o in facts["openings"]}


class _Shot:
    def __init__(self, name, facing):
        self.name, self.facing = name, facing


# ------------------------------------------------------------------------- the vetting ----
def test_the_blind_lid_port_is_not_offered_as_a_port():
    """The whole defect, on the real plate's own numbers: sticker 3 came off the card as `outlet_2`
    and a patch cut there has no faces."""
    facts = _facts()
    gc.vet_by_reach(facts)
    assert _roles(facts) == {1: "inlet", 2: "outlet", 3: "not_an_opening"}


def test_the_two_mouths_that_do_reach_the_outside_keep_their_roles():
    facts = _facts()
    gc.vet_by_reach(facts)
    kept = [o for o in facts["openings"] if o["role"] != "not_an_opening"]
    assert [o["id"] for o in kept] == [1, 2]
    assert [o["confidence"] for o in kept] == [0.99, 0.98], "a reaching mouth is not touched at all"


def test_the_card_says_which_opening_it_held_back_and_why():
    facts = _facts()
    gc.vet_by_reach(facts)
    note = next(n for n in facts["notes"] if "not an opening" in n)
    assert "opening 3 (outlet_2)" in note
    assert "straight ahead of it" in note
    # the user is the one who knows: a remedy, not a verdict
    assert "if you know better" in note


def test_the_pictures_may_not_score_a_mouth_the_ray_measured_blind():
    """The scout scores a non-reaching mouth 0.5 (`cad/fluid_volume_check`). The card showed 0.98."""
    facts = _facts()
    gc.vet_by_reach(facts)
    blind = next(o for o in facts["openings"] if o["id"] == 3)
    assert blind["confidence"] == gc.BLIND_MOUTH_CONFIDENCE == 0.5


def test_a_held_back_inlet_hands_the_role_to_the_largest_opening_left():
    """`vet_openings` and `vet_by_map` both do this; a part with no inlet has nothing to confirm."""
    openings = [dict(PLATE[0], role="inlet", reaches_outside=False, clear_ahead=False),
                dict(PLATE[1], role="outlet", area_mm2=50.27),
                dict(PLATE[2], role="outlet", reaches_outside=True, clear_ahead=True,
                     area_mm2=109.36)]
    facts = _facts(openings)
    gc.vet_by_reach(facts)
    assert _roles(facts) == {1: "not_an_opening", 2: "outlet", 3: "inlet"}
    assert _roles(facts)[3] == "inlet", "the largest of the mouths still reaching the outside"


# ------------------------------------------------- what the rule must NOT do ----
def test_a_mouth_nobody_measured_is_not_held_back():
    """None is "nobody measured it". `cad/scout_mesh` leaves it that way on the triangle path,
    where `clear_ahead` is only an alias for on_extremity - and `cad/scout._name_openings` was
    deliberately moved off `clear_ahead` for exactly this reason."""
    openings = [dict(o, reaches_outside=None, clear_ahead=False) for o in PLATE]
    facts = _facts(openings)
    gc.vet_by_reach(facts)
    assert _roles(facts) == {1: "inlet", 2: "outlet", 3: "outlet"}
    assert facts["notes"] == []


def test_an_opening_record_with_no_reach_field_at_all_is_not_held_back():
    """`geometry_check.map_openings` writes `clear_ahead: True` and no reach field at all, so a
    rule reading `clear_ahead` would be a silent no-op on that whole path. This one reads the
    field that is absent there, which is the honest answer: nobody measured it."""
    openings = [{k: v for k, v in o.items() if k != "reaches_outside"} for o in PLATE]
    facts = _facts(openings)
    gc.vet_by_reach(facts)
    assert _roles(facts) == {1: "inlet", 2: "outlet", 3: "outlet"}


def test_clear_ahead_alone_never_holds_a_mouth_back():
    """`clear_ahead` does not mean the same thing at its three producers. The engine's own refusal
    needs it AND a sealed rim (`mouths.Stub.is_sealed`); this one needs the measured reach."""
    openings = [dict(o, clear_ahead=False, reaches_outside=True) for o in PLATE]
    facts = _facts(openings)
    gc.vet_by_reach(facts)
    assert _roles(facts) == {1: "inlet", 2: "outlet", 3: "outlet"}


def test_a_vetting_never_leaves_fewer_than_two_mouths_reaching_the_outside():
    """A vetting cannot empty a part - `vet_openings` says so in as many words. With one mouth
    left there is no pair to offer instead, so the reading the part had stands and the user
    decides."""
    openings = [dict(PLATE[0]),
                dict(PLATE[1], clear_ahead=False, reaches_outside=False),
                dict(PLATE[2])]
    facts = _facts(openings)
    gc.vet_by_reach(facts)
    assert _roles(facts) == {1: "inlet", 2: "outlet", 3: "outlet"}
    assert facts["notes"] == []


def test_every_mouth_blind_changes_nothing():
    openings = [dict(o, clear_ahead=False, reaches_outside=False) for o in PLATE]
    facts = _facts(openings)
    gc.vet_by_reach(facts)
    assert _roles(facts) == {1: "inlet", 2: "outlet", 3: "outlet"}


def test_an_opening_already_not_a_port_is_left_where_it_is():
    openings = [dict(PLATE[0]), dict(PLATE[1]),
                dict(PLATE[2], role="not_an_opening", confidence=0.4)]
    facts = _facts(openings)
    gc.vet_by_reach(facts)
    assert _roles(facts) == {1: "inlet", 2: "outlet", 3: "not_an_opening"}
    assert facts["notes"] == [], "it was never offered, so there is nothing to say"
    assert facts["openings"][2]["confidence"] == 0.4


# ---------------------------------------------------- what the naming model is told ----
def test_the_naming_model_is_told_which_sticker_has_the_part_in_front_of_it():
    """It was being asked for this from the pictures, where it cannot be seen."""
    text = gc._facts_text(_facts(), [_Shot("overview", [1, 2, 3])], "")
    assert "3: circle opening, 12 mm across" in text
    assert "the part itself is straight ahead of this one" in text
    # and only of that one
    assert text.count("the part itself is straight ahead of this one") == 1


def test_a_sticker_that_reaches_the_outside_reads_exactly_as_it_did():
    text = gc._facts_text(_facts(), [_Shot("overview", [1, 2, 3])], "")
    assert "1: circle opening, 8 mm across" in text and "guessed inlet" in text
    line = next(ln for ln in text.splitlines() if ln.strip().startswith("1:"))
    assert line.rstrip().endswith("guessed inlet")


def test_the_model_is_instructed_to_answer_not_an_opening_for_such_a_sticker():
    assert "straight ahead of it" in gc._SYSTEM
    assert "not_an_opening for that sticker" in gc._SYSTEM


# ------------------------------------------- the pictures may not undo the measurement ----
def _vision(role_for_3: str = "outlet", conf: float = 0.98) -> dict:
    return {"part": "a liquid cold plate", "flow": "internal", "input_kind": "solid-assembly",
            "confidence": 0.9,
            "openings": [{"id": 1, "name": "inlet", "role": "inlet", "confidence": 0.99},
                         {"id": 2, "name": "outlet_1", "role": "outlet", "confidence": 0.98},
                         {"id": 3, "name": "outlet_2", "role": role_for_3, "confidence": conf}]}


def test_the_model_cannot_put_the_port_role_back_on_the_blind_mouth():
    """Without this the vetting is undone a few lines later: `_proposal` took any of the three
    enum words from the model with no geometric check of any kind."""
    facts = _facts()
    gc.vet_by_reach(facts)
    p = gc._merge(facts, _vision())
    by_id = {o["id"]: o for o in p["openings"]}
    assert by_id[3]["role"] == "not_an_opening"
    assert by_id[1]["role"] == "inlet" and by_id[2]["role"] == "outlet"


def test_the_model_cannot_raise_the_confidence_on_the_blind_mouth_either():
    """0.98 on the card against the measuring step's 0.5 was the whole of how it looked certain."""
    facts = _facts()
    gc.vet_by_reach(facts)
    p = gc._merge(facts, _vision())
    assert {o["id"]: o["confidence"] for o in p["openings"]}[3] == 0.5


def test_the_model_may_still_agree_that_it_is_not_an_opening():
    facts = _facts()
    gc.vet_by_reach(facts)
    p = gc._merge(facts, _vision(role_for_3="not_an_opening", conf=0.9))
    assert {o["id"]: o["role"] for o in p["openings"]}[3] == "not_an_opening"


def test_the_model_still_names_a_mouth_the_vetting_did_not_touch():
    """The guard is on the mouths the MEASUREMENT demoted, not on the model generally: where the
    vetting held nothing back the naming works exactly as before."""
    openings = [dict(o, reaches_outside=True, clear_ahead=True) for o in PLATE]
    facts = _facts(openings)
    gc.vet_by_reach(facts)
    p = gc._merge(facts, _vision(role_for_3="inlet", conf=0.97))
    by_id = {o["id"]: o for o in p["openings"]}
    assert by_id[3]["role"] == "inlet" and by_id[3]["confidence"] == 0.97


def test_a_blind_mouth_the_vetting_could_not_hold_back_is_still_the_models_to_name():
    """Only one mouth left reaching the outside, so the vetting stood aside; nothing here may then
    strip a role the code itself assigned."""
    openings = [dict(PLATE[0]),
                dict(PLATE[1], clear_ahead=False, reaches_outside=False),
                dict(PLATE[2])]
    facts = _facts(openings)
    gc.vet_by_reach(facts)
    p = gc._merge(facts, _vision())
    assert {o["id"]: o["role"] for o in p["openings"]}[3] == "outlet"


# ------------------------------------------------------- and out the other side ----
def test_the_declaration_the_intake_sends_carries_no_port_on_the_blind_mouth():
    """`geometry_confirmation.patches_from` drops a not_an_opening, so the held-back mouth reaches
    the builder as no declaration at all - and `mouths.port_rows(roles=False)` then cuts it as an
    unassigned opening, which is what job 6ddb58aa's refusal asked for."""
    from meshpipeline.application.geometry_confirmation import body_of, patches_from

    facts = _facts()
    gc.vet_by_reach(facts)
    patches = patches_from(body_of({"flow": "internal", "input_kind": "solid-assembly",
                                    "openings": [dict(o) for o in facts["openings"]]}))
    named = {p["name"]: p["type"] for p in patches if p["type"] != "wall"}
    assert "outlet_2" not in named
    assert named == {"inlet": "inlet", "outlet_1": "outlet"}


@pytest.mark.parametrize("role", ["inlet", "outlet"])
def test_either_role_is_held_back_on_a_blind_mouth(role):
    """`cad/scout._name_openings` sorts a non-reaching mouth last so it is rarely the inlet, but
    the rule is symmetric: a mouth with the part in front of it is not an end in either direction."""
    openings = [dict(PLATE[0], reaches_outside=False, clear_ahead=False, role=role),
                dict(PLATE[1]), dict(PLATE[2], reaches_outside=True, clear_ahead=True)]
    facts = _facts(openings)
    gc.vet_by_reach(facts)
    assert _roles(facts)[1] == "not_an_opening"


# -------------------- and the pictures may not undo the CONNECTIVITY measurement either ----
#
# THE SECOND CASE OF THE SAME PRINCIPLE, measured on session d2aa5820 (2026-11-27), this plate,
# driven as a customer end to end. `cad/fluid_volume_check._unname_a_severed_pair` had demoted all
# three mouths and said so in the notes - "they open into different spaces and nothing can flow
# from one to the other" - and the card the customer was shown STILL read inlet and outlet, at 0.65
# instead of the 0.98 and 0.96 the vision step gives unopposed, because `_merge` runs after the
# scout and hands any of the three enum words straight back.
#
# A render cannot see this any more than it can see a capped bore: no picture of a bonded plate
# shows that two of its mouths open into coolant spaces 0.580 mm apart with no face joining them.
# The previous run of this part, job 44362085, was refused by the engine 4 minutes 32 seconds after
# the customer pressed Proceed on exactly that pair.


def _severed(openings=None) -> dict:
    """The plate's mouths as the split leaves them once it has found the circuit severed: unnamed,
    typed `opening`, and MARKED - which is the only thing `_merge` can key off."""
    ops = [dict(o, name=f"mouth_{o['id']}", role="opening", severed_from_pair=True)
           for o in (openings or PLATE[:2])]
    return _facts(ops + [dict(PLATE[2])])


def test_the_pictures_cannot_put_a_port_pair_back_on_a_severed_circuit():
    """THE DEFECT. Without the hold the card offers the pair the engine refuses to mesh."""
    p = gc._merge(_severed(), _vision())
    by_id = {o["id"]: o for o in p["openings"]}
    assert by_id[1]["role"] == "opening", "the render handed the inlet role back"
    assert by_id[2]["role"] == "opening", "the render handed the outlet role back"
    # THE PAIR IS WHAT MATTERS, not every mouth on the card. `plan.ports_from` needs BOTH an inlet
    # and an outlet role to return a PortPlan at all, so a single remaining port role cannot make
    # the pair the engine refuses - and mouth 3 here is deliberately unmarked, because the reach
    # vetting is what deals with it and the two rules must not be tangled together.
    held = [o for o in p["openings"] if o.get("severed_from_pair")]
    assert len(held) == 2, held
    assert not [o for o in held if o["role"] in ("inlet", "outlet")], (
        "a port the split had unnamed reached the card as a port again")


def test_the_held_mouth_keeps_its_own_name_too():
    """A mouth called `inlet` and typed `opening` is the card contradicting itself again. The role
    is what travels - `plan.ports_from` keys off it - but the name is what a person reads."""
    p = gc._merge(_severed(), _vision())
    by_id = {o["id"]: o for o in p["openings"]}
    assert by_id[1]["name"] == "mouth_1" and by_id[2]["name"] == "mouth_2", (
        [o["name"] for o in p["openings"]])


def test_an_unmarked_mouth_is_named_by_the_pictures_exactly_as_before():
    """THE HALF THAT KEEPS EVERY OTHER PART WORKING. The mark is what holds a mouth; a plate whose
    circuit is whole carries no mark and the naming step decides as it always has."""
    p = gc._merge(_facts(), _vision())
    by_id = {o["id"]: o for o in p["openings"]}
    assert by_id[1]["role"] == "inlet" and by_id[1]["name"] == "inlet"
    assert by_id[2]["role"] == "outlet" and by_id[2]["name"] == "outlet_1"


def test_the_mark_alone_holds_it_whatever_the_reach_fields_say():
    """THE TWO RULES ANSWER DIFFERENT QUESTIONS and neither leans on the other. The blind hold is
    `reaches_outside is False and role == "not_an_opening"`; this one is the mark, and a severed
    mouth that reaches the outside perfectly well - which both 8 mm hose mouths of this plate do -
    has none of the blind rule's conditions. If the mark did not hold on its own, every severed
    pair on a part with no blind bore would go straight back to being a port pair.
    """
    ops = [dict(PLATE[0], name="mouth_1", role="opening", severed_from_pair=True,
                reaches_outside=True, clear_ahead=True),
           dict(PLATE[1], name="mouth_2", role="opening", severed_from_pair=True,
                reaches_outside=True, clear_ahead=True)]
    p = gc._merge(_facts(ops), _vision())
    by_id = {o["id"]: o for o in p["openings"]}
    assert by_id[1]["role"] == "opening" and by_id[2]["role"] == "opening", (
        [(o["id"], o["role"]) for o in p["openings"]])
    assert by_id[1]["name"] == "mouth_1" and by_id[2]["name"] == "mouth_2"
