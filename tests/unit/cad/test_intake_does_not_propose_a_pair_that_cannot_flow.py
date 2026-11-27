# Responsibility: Verify intake stops proposing an inlet/outlet pair that its own connectivity
# reading says cannot flow, and still proposes one whenever the two ends do connect.
# Boundaries: the proposal only. The hard refusal stays where it is
# (cad/fluid_passages.ports_on_one_passage, which the engine runs before it starts).
#
# MEASURED, job 44362085 (2026-11-27), the whole product on the customer's own
# ColdPlateAssembly_CLEAN.step, driven as a customer: upload, one sentence of purpose, Proceed on
# the proposal UNEDITED, "Water, about 1 litre a minute", and the engine the product chose itself.
#
# Intake read the part correctly. Its own notes said, in this order:
#
#     the fluid is the space inside the 6 parts, closed off at 3 openings (54 ml)
#     the parts close it into 2 spaces that do not connect (space 1: openings 1, 3; space 2:
#       opening 2) - a valve drawn shut, or a part sealing the passage; each space is meshed on
#       its own
#
# and then proposed, on the same card, opening 1 as the inlet (confidence 0.98) and opening 2 as
# the outlet (0.96) - the two it had just said do not connect. Four minutes and thirty-two seconds
# later the engine refused, correctly:
#
#     refusing to start - the inlet and the outlet open into different pieces of the coolant,
#     0.580 mm apart at their closest, and no face joins them: inlet opens into a piece of
#     52,140.3 mm3 and outlet into one of 1,496.8 mm3, out of 2 the surface is in.
#
# THE HARD REFUSAL IS NOT THE DEFECT and is not moved. `ports_on_one_passage` reads the surface the
# mesher is about to fill and its own note says why it runs there: "it costs milliseconds on a
# surface that costs hours to mesh". The defect is that the proposal a customer is asked to confirm
# contradicted the sentence printed directly above it, from the same measurement.
from __future__ import annotations

import inspect

from meshpipeline.cad.fluid_volume_check import _unname_a_severed_pair, fold, read_fluid_volume

#: Job 44362085's own three openings, as intake proposed them, with the spaces it put them in.
#: Openings 1 and 3 are in space 0 and opening 2 is in space 1 - the plate's severed circuit.
SEVERED_SPACES = {1: 0, 2: 1, 3: 0}


def _openings(*roles: tuple[int, str, str]) -> list[dict]:
    return [{"id": i, "name": n, "role": r} for i, n, r in roles]


def test_the_pair_that_cost_job_44362085_its_run_is_no_longer_proposed():
    """THE DEFECT, with the run's own numbers."""
    ops = _openings((1, "inlet", "inlet"), (2, "outlet", "outlet"),
                    (3, "not_an_opening", "not_an_opening"))
    notes: list = []
    _unname_a_severed_pair(ops, SEVERED_SPACES, notes)

    assert [o["role"] for o in ops] == ["opening", "opening", "not_an_opening"], (
        "the two mouths that cannot flow to each other are still proposed as a flow pair")
    assert [o["name"] for o in ops] == ["mouth_1", "mouth_2", "not_an_opening"]
    # THE PATH THIS LANDS ON IS THE ONE WITH A DELIVERED MESH BEHIND IT: with no inlet and no
    # outlet role among the port patches, `plan.ports_from` returns None and `mouths.port_rows`
    # types every mouth `opening` as `mouth_<k>` - which is what job 606af55e did on this part.
    assert not [o for o in ops if o["role"] in ("inlet", "outlet")]
    said = " ".join(notes)
    assert "left unnamed" in said, said
    assert "nothing can flow from one to the other" in said, said
    # AND IT SAYS WHICH ONES, because a customer who disagrees has to know what to put back.
    assert "inlet (inlet)" in said and "outlet (outlet)" in said, said
    # ...AND THAT THE MESH IS STILL COMING, which is the whole point of not refusing here.
    assert "still meshed together" in said, said


def test_a_pair_on_one_space_is_left_exactly_as_it_was():
    """THE ORDINARY PART, which is most of them: two ends of one passage, and nothing moves.

    This is the half that makes the change safe. Every corpus part whose inlet and outlet are on
    the same space keeps the proposal it has always had, byte for byte.
    """
    ops = _openings((1, "inlet", "inlet"), (2, "outlet", "outlet"))
    before = [dict(o) for o in ops]
    notes: list = []
    _unname_a_severed_pair(ops, {1: 0, 2: 0}, notes)
    assert ops == before, "a sound pair was disturbed"
    assert notes == [], notes


def test_one_port_alone_is_not_a_pair():
    """A single named mouth cannot contradict a connectivity reading, so it is left alone - the
    engine's own note is that "a sealed part is still worth meshing" and one end is the same case.
    """
    for role in ("inlet", "outlet"):
        ops = _openings((1, role, role), (2, "not_an_opening", "not_an_opening"))
        before = [dict(o) for o in ops]
        notes: list = []
        _unname_a_severed_pair(ops, SEVERED_SPACES, notes)
        assert ops == before, role
        assert notes == []


def test_a_mouth_with_no_space_recorded_cannot_sever_anything():
    """`space_of` is keyed by sticker and a mouth missing from it is a mouth whose space nobody
    measured - which is not evidence that it is in a different one."""
    ops = _openings((1, "inlet", "inlet"), (2, "outlet", "outlet"))
    before = [dict(o) for o in ops]
    notes: list = []
    _unname_a_severed_pair(ops, {1: 0}, notes)
    assert ops == before
    assert notes == []


def test_it_is_actually_WIRED_into_the_reading_that_proposes_the_pair():
    """THE TEST THE FIVE ABOVE DO NOT MAKE, and the omission is worth naming: every one of them
    calls `_unname_a_severed_pair` directly, so all five pass with the call site deleted. I checked,
    by deleting it. A guard on a function nobody calls guards nothing.

    Driving it end to end needs a real CAD read, so what is asserted is the SOURCE - the same shape
    `tests/unit/application/test_the_delivered_bundle_is_not_compressed_on_one_core` uses on
    `_build_plan`, for the same reason. `fold` is the function that writes the notes and settles the
    proposal; `read_fluid_volume` is the reading that calls it, and BOTH links are checked, because
    a guard wired into a function nobody reaches is the same as no guard.
    """
    chain = inspect.getsource(read_fluid_volume)
    assert "fold(" in chain, (
        "the reading no longer folds the answer into the facts, so the guard below is unreachable")

    src = inspect.getsource(fold)
    assert "_unname_a_severed_pair(openings, space_of, notes)" in src, (
        "the function that settles the proposal does not call the guard, so nothing is guarded")
    where = src.index("_unname_a_severed_pair")
    note = src.index("spaces that do not connect")
    assert note < where, (
        "the guard runs before the note that lists the spaces, so the card would say the pair is "
        "unnamed before it says why")
    # INSIDE THE ONE BRANCH WHERE TWO SPACES CAN EXIST. Outside it `space_of` holds a single value
    # for every mouth, so the guard could never fire and would read as dead code.
    branch = src.rindex("if len(passages) > 1:", 0, where)
    assert (where - branch) < 900, (
        "the guard is not inside the branch that knows the part has more than one space")


def test_three_ports_across_two_spaces_are_all_unnamed():
    """A part really drawn as two circuits proposes more than two ports, and leaving one named
    would ship a lone inlet - `plan.ports_from` would then return a PortPlan with an empty outlet,
    which is the shape that delivered a mesh nothing anywhere fixes the pressure of (job b6762d5c).
    """
    ops = _openings((1, "inlet", "inlet"), (2, "outlet", "outlet"), (3, "inlet_2", "inlet"))
    notes: list = []
    _unname_a_severed_pair(ops, {1: 0, 2: 1, 3: 0}, notes)
    assert [o["role"] for o in ops] == ["opening"] * 3, [o["role"] for o in ops]
    assert [o["name"] for o in ops] == ["mouth_1", "mouth_2", "mouth_3"]
