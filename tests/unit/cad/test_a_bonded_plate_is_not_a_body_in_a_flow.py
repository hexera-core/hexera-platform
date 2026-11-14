"""A bonded plate whose coolant the split found is not a body in a flow.

MEASURED END TO END on ColdPlateAssembly_CLEAN.step, session eb22296d, 2026-11-13, driving the
product as a user. The split cut 52.1 ml of coolant out of the six solids and proposed its three
mouths, and the reading came back `flow: external`, `input_kind: solid-body` - the fluid goes
AROUND a liquid cold plate. What that cost, in order:

    facts["flow"] == "external"
      -> geometry_check.stickered() returns [] for an external reading
      -> render_snapshots draws NO stickers and NO per-opening close-ups
         (the session stored overview/iso/top/front/side/end and nothing else)
      -> the naming model, shown six unmarked pictures, answered in its own notes:
         "Two external hose/port mouths are visible on the side, but neither has a numbered
          sticker, so they cannot be assigned inlet/outlet names here" - and returned openings: []
      -> the scout's mechanical default stood: _name_openings gives the WIDEST of three mouths
         the inlet, which is the 11.8 mm junction sealed inside the assembly, and called both real
         8 mm mouths outlets.

The rule that stops this already existed and was written for this exact part - "`single_solid` is
the cold plate: a solid body, with coolant found inside it. That one is a conjugate case whatever
else is true, and it is the one that was being read as a car" - but it sat on the ONE-SOLID branch
of fold(), which a six-solid brazed plate never reaches. On the assembly branch the flow was set
only where it was already internal, or where the scout had read a surface; a part the scout called
a solid body in a flow kept that reading however much coolant came out of it.
"""
from __future__ import annotations

from meshpipeline.cad.fluid_volume_check import fold

#: The three mouths of the real plate, as the split proposed them (diameters 11.8, 8.0, 8.0 mm).
_MOUTHS = [
    (0, [65.92, 33.45, 75.41], [0.0, 1.0, 0.0], 109.36),
    (0, [99.88, 54.68, 43.31], [0.99823, 0.0, 0.05941], 50.27),
    (1, [89.34, 54.68, 77.39], [0.99643, 0.0, 0.08438], 50.27),
]


def _plate_facts(**over) -> dict:
    """What the scout had measured on the plate before the split's answer was folded in."""
    facts = {"solids": 6, "body_kind": "single_solid", "input_kind": "solid-body",
             "flow": "external", "read_as": "cad",
             "openings": [{"id": i, "role": "inlet"} for i in (1, 2, 3, 4, 5)],
             "notes": [], "confidence": {"input_kind": 0.7, "openings": 0.85}}
    facts.update(over)
    return facts


def _cap(centroid, normal, area_mm2) -> dict:
    return {"centroid_mm": list(centroid), "normal": list(normal), "area_mm2": area_mm2,
            "hole_mm2": area_mm2, "wh_mm": [0.0, 0.0], "on_extremity": True,
            "clear_ahead": True, "gap": None, "proposed": True}


def _plate_answer() -> dict:
    """The split's own answer on the plate: 6 parts, 58 caps, two spaces of 52,144.56 and
    1,517.23 mm3, open at three places between them."""
    spaces = [[], []]
    for k, centroid, normal, area in _MOUTHS:
        spaces[k].append(_cap(centroid, normal, area))
    return {"found": True, "parts": 6, "caps": 58, "regions": 70, "seconds": 158.8,
            "pockets_left_out": 1, "leaks_mm": [],
            "passages": [{"volume_l": 0.05214456, "volume_mm3": 52144.5599,
                          "seed_mm": [65.92, 30.5, 75.41], "openings": spaces[0]},
                         {"volume_l": 0.001517227, "volume_mm3": 1517.227,
                          "seed_mm": [87.35, 54.68, 77.22], "openings": spaces[1]}]}


# #
# the reading
# #
def test_coolant_cut_out_of_a_bonded_plate_makes_it_a_cooled_solid_not_a_body():
    facts = _plate_facts()
    fold(facts, _plate_answer())
    assert facts["flow"] == "internal", "52 ml of coolant came out of it, open at three places"
    assert facts["input_kind"] == "solid-assembly"
    assert facts["confidence"]["input_kind"] == 0.85
    assert facts["openings_from"] == "fluid_volume"


def test_the_plate_now_wears_the_stickers_the_naming_step_is_asked_about():
    """The whole point of the reading: an external part is drawn WITHOUT stickers
    (geometry_check.stickered), and the naming step is a vision model looking at the pictures."""
    from meshpipeline.application.geometry_check import stickered

    before = _plate_facts()
    assert stickered(before) == [], "the reading as it was: six unmarked pictures"
    facts = _plate_facts()
    fold(facts, _plate_answer())
    assert [o["id"] for o in stickered(facts)] == [1, 2, 3]


def test_the_six_solid_plate_gets_the_same_answer_as_the_one_solid_plate():
    """One block with channels bored through it and six plates brazed into one are the same part
    to a mesher, and the rule was only ever on the one-solid branch."""
    one = {"solids": 1, "body_kind": "single_solid", "input_kind": "solid-body", "flow": "external",
           "openings": [{"id": i, "role": "inlet"} for i in (1, 2)], "notes": [], "confidence": {}}
    fold(one, _plate_answer())
    many = _plate_facts()
    fold(many, _plate_answer())
    assert (one["flow"], one["input_kind"]) == (many["flow"], many["input_kind"]) \
        == ("internal", "solid-assembly")


# #
# what the rule must NOT reach
# #
def test_metal_around_a_fluid_is_still_an_internal_case_and_not_a_conjugate_one():
    """"...FOR A SOLID BODY, AND NOT FOR A WALL" - the one-solid rule's own words. A part the
    scout read as pipe_wall or hollow_wall is metal AROUND a fluid and its ordinary case is the
    fluid alone; reading a valve or a tee as solid-assembly would impose a two-region case on the
    commonest internal-flow part there is."""
    for wall in ("pipe_wall", "hollow_wall"):
        facts = _plate_facts(body_kind=wall, flow="internal")
        fold(facts, _plate_answer())
        assert facts["flow"] == "internal"
        assert facts["input_kind"] == "body-surface", wall
        assert facts["body_kind"] == "assembly_cavity", wall


def test_a_file_of_surfaces_keeps_the_cavity_reading_it_had():
    facts = _plate_facts(body_kind="surface", solids=0)
    fold(facts, _plate_answer())
    assert (facts["flow"], facts["input_kind"]) == ("internal", "body-surface")
    assert facts["body_kind"] == "assembly_cavity"


def test_a_split_that_finds_no_coolant_leaves_the_body_reading_alone():
    facts = _plate_facts()
    fold(facts, {"found": False, "reason": "no space inside the parts"})
    assert (facts["flow"], facts["input_kind"]) == ("external", "solid-body")
    assert "openings_from" not in facts


def test_one_mouth_is_no_flow_through_and_changes_nothing():
    """A passage needs a way in and a way out: fewer than two proposed caps and the reading the
    part had stands, body or not."""
    answer = _plate_answer()
    answer["passages"] = [{"volume_l": 0.001, "volume_mm3": 1000.0, "seed_mm": [1.0, 1.0, 1.0],
                           "openings": [_cap([99.88, 54.68, 43.31], [1.0, 0.0, 0.0], 50.27)]}]
    facts = _plate_facts()
    fold(facts, answer)
    assert (facts["flow"], facts["input_kind"]) == ("external", "solid-body")


# #
# and the same thing said where the pictures are drawn
# #
def test_a_mouth_the_split_measured_is_stickered_whatever_the_flow_was_read_as():
    """The reading above is one guard and this is the other, because they are about different
    openings. A body in a flow keeps its own flat FACES as the openings it would have if the fluid
    flowed through it, and drawing stickers on a shaft's two ends would tell the naming model there
    are passages where there are none. An opening carrying `openings_from == "fluid_volume"` is a
    cap where a fluid volume the split cut out of this part meets the outside, so an external
    reading beside it is a contradiction inside one set of facts."""
    from meshpipeline.application.geometry_check import stickered

    faces = {"flow": "external", "openings": [{"id": 1}, {"id": 2}]}
    assert stickered(faces) == [], "a body's own flat ends stay undrawn, as the sweep decided"
    measured = dict(faces, openings_from="fluid_volume")
    assert [o["id"] for o in stickered(measured)] == [1, 2]


def test_the_plate_is_guarded_twice_over():
    from meshpipeline.application.geometry_check import stickered

    facts = _plate_facts()
    fold(facts, _plate_answer())
    assert facts["flow"] == "internal" and facts["openings_from"] == "fluid_volume"
    assert len(stickered(facts)) == 3
    assert len(stickered(dict(facts, flow="external"))) == 3


# #
# and what the naming step is told about where the stickers came from
# #
def _said(facts: dict) -> str:
    from meshpipeline.application.geometry_check import _facts_text

    base = {"size_mm": [177.5, 41.84, 101.0], "body_kind": "single_solid", "openings": [],
            "confidence": {}}
    return _facts_text({**base, **facts}, [], "")


_DECLARED = "The file itself declares this:"
_SPLIT = "The stickers are not read off the part's outside."


def test_a_plate_is_not_told_its_stickers_are_settled_by_a_file_that_declares_nothing():
    """`solid-assembly` reaches the facts two ways. An ECXML thermal model DECLARES its regions,
    its air and its openings. A cold plate gets the same kind because a SPLIT found coolant inside
    it - the file declares nothing, there is no air around anything, and "the kind, the flow and
    the stickers are settled by the file" tells the model not to look."""
    plate = _said({"input_kind": "solid-assembly", "flow": "internal",
                   "openings_from": "fluid_volume"})
    assert _DECLARED not in plate
    assert "the air around them" not in plate
    assert _SPLIT in plate and "there IS a passage through this part" in plate


def test_a_file_that_does_declare_its_regions_is_told_so_word_for_word():
    declared = _said({"input_kind": "solid-assembly", "flow": "internal",
                      "source_regions": ["a", "b", "c"]})
    assert "The file itself declares this: an assembly of 3 regions" in declared
    assert "settled by the file" in declared
    assert _SPLIT not in declared


def test_a_thermal_model_keeps_the_sentence_it_had():
    from meshpipeline.contracts.intake_formats import THERMAL_CHECK

    thermal = _said({"input_kind": "solid-assembly", "flow": "internal",
                     "check_mode": THERMAL_CHECK})
    assert _DECLARED in thermal and "settled by the file" in thermal

