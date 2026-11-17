# Responsibility: Verify the one flow-kind authority (application/flow_kind, FLOW-1..5) on parts built
# on paper and run through the triangle check as the worker runs it: what the shape decides, where it
# cannot tell and says "undecided" with the one question, and that the user's words and the file's own
# declaration always win, the pictures only where they are clearly surer.
# Boundaries: numpy meshes written as STL through geometry_check._scout_triangles (no CAD kernel, no
# model, no storage), and plain facts dicts for the weighing.
from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from meshpipeline.application import flow_kind as fk
from meshpipeline.application import geometry_check as gc
from meshpipeline.cad.stl_io import write_stl_binary


# ------------------------------------------------------------------------- parts on paper ----
def _ring(r, z, n=40):
    return [(r * math.cos(2 * math.pi * i / n), r * math.sin(2 * math.pi * i / n), z) for i in range(n)]


def _band(a, b):
    tris = []
    for i in range(len(a)):
        j = (i + 1) % len(a)
        tris += [(list(a[i]), list(a[j]), list(b[j])), (list(a[i]), list(b[j]), list(b[i]))]
    return tris


def _fan(centre, ring, flip=False):
    tris = []
    for i in range(len(ring)):
        a, b = ring[i], ring[(i + 1) % len(ring)]
        tris.append((list(centre), list(b), list(a)) if flip else (list(centre), list(a), list(b)))
    return tris


def _revolved(profile, *, capped=True):
    """A body of revolution about z from (z, r) pairs, its two ends closed by flat discs (or left open)."""
    rings = [_ring(r, z) for z, r in profile]
    tris = []
    for a, b in zip(rings, rings[1:]):
        tris += _band(a, b)
    if capped:
        tris += _fan((0, 0, profile[0][0]), rings[0], flip=True) + _fan((0, 0, profile[-1][0]), rings[-1])
    return tris


def _check(tmp_path, name, tris) -> dict:
    p = tmp_path / f"{name}.stl"
    write_stl_binary(p, tris)
    work = tmp_path / f"work_{name}"
    work.mkdir()
    ref = SimpleNamespace(owner_id="o", source_id="s", suffix_hint=".stl", original_filename=f"{name}.stl")
    facts, _skin = gc._scout_triangles(p, work, None, ref)
    return facts


# ----------------------------------------------------------- what the shape alone decides ----
def test_a_straight_solid_whose_ends_are_its_whole_section_is_the_fluid_in_a_pipe(tmp_path):
    """A stepped round solid with flat ends (a reducer's fluid) reads as the fluid in a straight pipe."""
    facts = _check(tmp_path, "reducer", _revolved([(0, 40), (300, 40), (340, 25), (600, 25)]))
    rec = facts["flow_kind"]
    assert rec["kind"] == "internal" and rec["confidence"] >= fk.DECIDE_AT
    assert facts["flow"] == "internal" and facts["flow_scout"] == "internal"
    assert rec["reasons"] and rec["question"] == ""


def test_a_straight_solid_that_bulges_past_its_flat_ends_is_undecided(tmp_path):
    """A model on a sting, or the fluid of a chamber between two pipes: the shape cannot tell, so the
    product asks the one question and draws its best guess meanwhile (main read it as the fluid)."""
    facts = _check(tmp_path, "sting", _revolved([(0, 20), (200, 20), (260, 120), (700, 120), (760, 20), (1000, 20)]))
    rec = facts["flow_kind"]
    assert rec["kind"] == "undecided"
    assert rec["question"] == fk.QUESTION == "Is the flow through the inside or around the outside?"
    assert rec["flow"] in ("internal", "external") and rec["best_guess"] == rec["flow"]
    assert facts["flow"] == rec["flow"]
    assert any("cannot tell" in r for r in rec["reasons"])


def test_a_flat_patch_with_no_thickness_is_undecided(tmp_path):
    """One boundary patch (an inlet disc exported alone): nothing flows through or around a patch by
    itself (main called it a body in a flow)."""
    inner, outer = _ring(25.0, 0.0), _ring(50.0, 0.0)
    facts = _check(tmp_path, "patch", _fan((0, 0, 0), inner) + _band(inner, outer))
    assert facts["flow_kind"]["kind"] == "undecided"


def test_an_open_skin_with_open_ends_is_undecided(tmp_path):
    """A zero-thickness tube open at both ends: a duct's wall, or a body's skin open at its ends (a
    wing skin open at root and tip) - the shape cannot tell (main called it a passage)."""
    facts = _check(tmp_path, "skin", _revolved([(0, 50), (1000, 50)], capped=False))
    assert facts["flow_kind"]["kind"] == "undecided"


def test_a_pipe_wall_with_a_way_through_is_internal(tmp_path):
    """A thick-walled tube: ring ends leading into a passage the inside-space map finds."""
    outer, inner = _ring(50.0, 0.0), _ring(40.0, 0.0)
    outer1, inner1 = _ring(50.0, 800.0), _ring(40.0, 800.0)
    tris = _band(outer, outer1) + _band(inner1, inner) + _band(inner, outer) + _band(outer1, inner1)
    rec = _check(tmp_path, "wall", tris)["flow_kind"]
    assert rec["kind"] == "internal" and rec["confidence"] >= 0.85


def test_a_closed_round_body_with_no_flat_ends_is_external(tmp_path):
    rec = _check(tmp_path, "blob", _revolved([(0, 0.01), (50, 60), (300, 80), (600, 40), (700, 0.01)]))["flow_kind"]
    assert rec["kind"] == "external"


# ------------------------------------------------------------------- what wins, in order ----
def _straight_fluid() -> dict:
    return {"body_kind": "single_solid", "input_kind": "fluid-domain", "flow": "internal", "solids": 1,
            "size_mm": [100.0, 100.0, 1000.0], "confidence": {"input_kind": 0.7},
            "inside": {"rule": "INS-1", "has_inside": False, "passages": 0, "mouths_per_passage": [],
                       "enclosed_mm3": 2.0e6},
            "openings": [{"id": 1, "role": "inlet", "diameter_mm": 30.0, "centroid_mm": [0, 0, 0], "normal": [0, 0, -1]},
                         {"id": 2, "role": "outlet", "diameter_mm": 30.0, "centroid_mm": [0, 0, 1000], "normal": [0, 0, 1]}]}


def test_the_users_stated_flow_beats_the_shape_both_ways():
    facts = _straight_fluid()
    assert fk.decide(facts)["kind"] == "undecided"
    around = fk.decide(facts, words="the air flows around it at 30 m/s")
    assert (around["kind"], around["by"], around["input_kind"]) == ("external", "words", "solid-body")
    through = fk.decide(facts, words="water flows through it")
    assert (through["kind"], through["by"], through["input_kind"]) == ("internal", "words", "fluid-domain")
    # a pipe wall the map reads as a passage still follows the words, with the shape's reading said
    wall = dict(facts, body_kind="pipe_wall", input_kind="body-surface",
                inside={"rule": "INS-1", "has_inside": True, "mouths_per_passage": [2], "enclosed_mm3": 5e5})
    rec = fk.decide(wall, words="external aero, flow around the part")
    assert rec["kind"] == "external" and any("your words win" in r for r in rec["reasons"])


def test_the_object_the_user_names_decides_an_undecided_shape():
    rec = fk.decide(_straight_fluid(), words="mesh this exhaust manifold")
    assert (rec["kind"], rec["by"]) == ("internal", "words")
    assert fk.decide(_straight_fluid(), words="a race car")["kind"] == "external"


def test_the_pictures_decide_only_when_clearly_surer_and_contradicting_nothing():
    facts = _straight_fluid()
    sure = {"part": "a rod", "flow": "external", "input_kind": "solid-body", "confidence": 0.9}
    assert fk.decide(facts, vision=sure)["kind"] == "external"
    assert fk.decide(facts, vision=dict(sure, confidence=0.55))["kind"] == "undecided"
    # a part with fewer than two openings cannot be flowed through, whatever the pictures say
    one = dict(facts, openings=facts["openings"][:1], body_kind="single_solid", input_kind="solid-body", flow="external")
    rec = fk.decide(one, vision={"part": "a pipe", "flow": "internal", "input_kind": "fluid-domain", "confidence": 1.0})
    assert rec["flow"] == "external" and any("measured reading stands" in r for r in rec["reasons"])


def test_a_file_that_declares_its_regions_is_internal_whatever_else_is_said():
    facts = {"check_mode": "thermal", "input_kind": "solid-assembly", "flow": "internal", "openings": []}
    rec = fk.decide(facts, words="air flows around the board")
    assert (rec["kind"], rec["rule"]) == ("internal", "FLOW-5")


def test_the_answer_to_the_one_question_decides():
    facts = _straight_fluid()
    rec = fk.decide(facts)
    assert fk.answer(rec, facts, "The flow goes around the outside")["kind"] == "external"
    assert fk.answer(rec, facts, "The flow goes through the inside")["kind"] == "internal"
    assert fk.answer(rec, facts, "ok")["kind"] == "undecided"


def test_the_value_has_the_shape_the_intake_reads():
    rec = fk.decide(_straight_fluid())
    assert set(rec) >= {"kind", "flow", "best_guess", "confidence", "by", "rule", "reasons", "question", "input_kind"}
    assert rec["kind"] in ("internal", "external", "undecided") and 0.0 <= rec["confidence"] <= 1.0
    assert all(isinstance(r, str) and r for r in rec["reasons"])


def test_a_proposal_carries_the_same_verdict_and_the_naming_stamps_it_on_the_facts():
    facts = _straight_fluid()
    gc.settle_flow_kind(facts)
    proposal = gc._proposal(facts, None, words="the air flows around it")
    assert proposal["flow_kind"]["kind"] == "external" and proposal["flow"] == "external"
    gc.stamp_flow_kind(facts, proposal["flow_kind"])
    assert facts["flow_kind"]["kind"] == "external" and facts["flow_scout"] == "internal"
    # the measuring step's own reading is what the authority weighs, however often it is re-decided
    assert fk.decide(facts)["kind"] == "undecided"


@pytest.mark.parametrize("broken", [None, {}, {"openings": "x"}, {"size_mm": "big"}])
def test_a_reading_that_cannot_be_made_is_undecided_never_an_error(broken):
    rec = fk.decide(broken)  # type: ignore[arg-type]
    assert rec["kind"] in ("undecided", "internal", "external")
