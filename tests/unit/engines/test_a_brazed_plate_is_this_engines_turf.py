# Responsibility: Verify the conjugate mesher's home turf is read off whether the coolant is the
# space INSIDE the part, not off how many solids the file holds.
# Boundaries: declarations and the turf verdict only; no geometry, no mesher, no model.
#
# WHY THIS FILE EXISTS. The first real customer part this engine was ever shown - a Corintis cold
# plate, six solids brazed into one piece with 52.14 ml of coolant cut out of it cleanly - put the
# engine OUTSIDE its own turf on `n_solids >= 2`, and the recommender offered the electronics snap
# grid for a STEP cold plate instead. A brazed or diffusion-bonded plate is a base and a lid, which
# is the commonest way one is made, and the engine's own confirmation wording had said so all along.

from meshpipeline.engines.capability import FLOW_MULTI_REGION
from meshpipeline.engines.home_turf import TURF_FACTS, row_problems, verdict
from meshpipeline.engines.registry import get_spec

SPEC = get_spec("cfmesh_conjugate")

#: The customer plate as the check measured it: six solids, closed=False off a welded tessellation
#: that skipped two null triangulations, and the split found the coolant anyway.
CORINTIS = {"flow": FLOW_MULTI_REGION, "form": "cad", "input_kind": "solid-assembly",
            "n_solids": 6, "closed": False, "coolant_inside": True}


def _turf(**over):
    return verdict(SPEC, FLOW_MULTI_REGION, {**CORINTIS, **over}).severity


def test_the_declaration_is_well_formed():
    assert row_problems(SPEC) == []
    assert "coolant_inside" in TURF_FACTS, (
        "the fact the rows now turn on is not declared, so nothing says where it comes from")


def test_a_brazed_plate_of_six_solids_is_home():
    assert _turf() == "home", (
        "six solids bonded into one piece with coolant cut out of them is this engine's case")


def test_one_solid_with_passages_is_still_home():
    assert _turf(n_solids=1, coolant_inside=True) == "home"


def test_a_single_solid_on_its_own_claims_nothing_now_that_the_loose_row_is_gone():
    """`TurfRow("n_solids", "==", 1, "home")` was removed and this is the reading that shows it.

    THE TEST THAT USED TO SIT HERE ASSERTED THE RULE THE CHANGE REMOVED, kept passing, and said
    "the older rule stands on its own: it needs no extraction to be true". It passed because
    `verdict` (engines/home_turf.py) returns TURF_HOME whenever no outside or weak row HOLDS -
    including when every row is unmeasured. MEASURED 2026-11-12 on these exact facts, with the row
    present and with it absent: severity "home" both times, strengths [] both times,
    unmeasured [coolant_inside, cells_across_at_budget] both times. A severity assertion here
    cannot fail, whatever the declaration says.

    WHAT THE RANKER READS IS WHETHER ANYTHING HELD: engines/fitness.recommend blanks the turf
    when nothing did. So that is what is asserted. With the row back, n_solids HOLDS on these
    facts and the engine claims home on a part nobody has looked inside - a plate with open
    grooves and no lid is one solid with no enclosed coolant at all, which this engine's own
    advisory says it cannot mesh.
    """
    facts = {k: v for k, v in CORINTIS.items() if k != "coolant_inside"}
    got = verdict(SPEC, FLOW_MULTI_REGION, {**facts, "n_solids": 1})
    held = got.strengths + got.weak_spots + got.outside
    assert not held, f"one solid alone held {[r.fact for r in held]}"
    assert "coolant_inside" in got.unmeasured, "the missing fact is not reported as missing"
    assert not any(r.fact == "n_solids" for r in SPEC.home_turf), (
        "the solid-count row is back, so one solid claims this engine's turf off something that "
        "is not the coolant")


def test_a_file_whose_fluid_is_a_body_is_outside():
    """An electronics model or an air-box assembly: the regions are already there to mesh."""
    assert _turf(coolant_inside=False) == "outside"


def test_an_unmeasured_part_makes_no_claim_either_way():
    """A check that has not run is not evidence either way, and must not be read as either.

    Before this, an unmeasured multi-solid part was ruled OUT on the solid count alone; after it,
    the danger is the opposite - claiming a part the split has never looked at. Every row goes
    quiet instead.

    ASSERTED ON WHAT HELD, not on `severity`. `verdict` falls back to "home" when nothing holds,
    so severity alone cannot tell a part this engine is for from a part it knows nothing about.
    `fitness` is the one that reads it, and it reads exactly this: `turf = v.severity if held
    else ""` (engines/fitness.py). Measured end to end on the customer plate with the check not
    run: the fit comes out blank and the snap grid is recommended.
    """
    facts = {k: v for k, v in CORINTIS.items() if k != "coolant_inside"}
    got = verdict(SPEC, FLOW_MULTI_REGION, facts)
    held = got.strengths + got.weak_spots + got.outside
    assert not held, f"an unmeasured part held {[r.fact for r in held]}; it should claim nothing"
    assert "coolant_inside" in got.unmeasured, "the missing fact is not reported as missing"


def test_what_the_recommender_shows_for_each_of_the_three_readings():
    """The claim as the only reader of it makes it (engines/fitness.py: severity if held else "")."""
    def shown(**over):
        got = verdict(SPEC, FLOW_MULTI_REGION, {**CORINTIS, **over})
        return got.severity if (got.strengths + got.weak_spots + got.outside) else ""

    facts_unmeasured = {k: v for k, v in CORINTIS.items() if k != "coolant_inside"}
    got = verdict(SPEC, FLOW_MULTI_REGION, facts_unmeasured)
    assert shown(coolant_inside=True) == "home"
    assert shown(coolant_inside=False) == "outside"
    assert (got.severity if (got.strengths + got.weak_spots + got.outside) else "") == ""


def test_the_solid_count_alone_never_rules_this_engine_out():
    """The row that cost it the customer part. Six solids, nothing else known, is not a refusal."""
    for n in (2, 3, 6, 19):
        facts = {"flow": FLOW_MULTI_REGION, "form": "cad", "input_kind": "solid-assembly",
                 "n_solids": n}
        assert verdict(SPEC, FLOW_MULTI_REGION, facts).severity != "outside", (
            f"{n} solids is read as outside this engine's turf on the count alone")


def test_an_unclosed_solid_does_not_refuse_a_part_the_split_succeeded_on():
    """`closed` is read off a welded tessellation; `coolant_inside` is read off the faces.

    The customer plate came back closed=False from two skipped null triangulations while the split
    cut 52.14 ml out of it with no leaks at all. Almost any real assembly with fittings reads the
    same way, so a `closed` row is a penalty on every honest part rather than a test that tells
    parts apart - and as `weak` it was no better, because weak outranks home in `verdict`.
    """
    assert _turf(closed=False) == "home"
    assert not any(r.fact == "closed" for r in SPEC.home_turf), (
        "a `closed` row is back; it answers the same question as coolant_inside and answers it "
        "off a tessellation rather than off the faces")


def test_too_few_cells_across_is_still_weak():
    """The one weak row that is about the mesh rather than the reading survives untouched."""
    assert _turf(cells_across_at_budget=3.0) == "weak"


def test_the_cells_across_row_stops_where_its_own_evidence_stops():
    """IT WAS `< 4.0` AND ITS OWN SENTENCE CITES 2 AND 3.

    The row reads "Measured on eleven cold plates, the coolant's volume error at 2 and 3 cells
    across runs to 27% and 32%". Four was an extrapolation, and `weak` is the heaviest instrument
    in this file - home_turf.verdict lets one weak row beat every home row, which is exactly why
    the `closed` row was removed. At 4.0 the row demoted the only engine that can mesh a sealed
    cold plate for being a hundredth of a cell under a bar nothing had measured.

    AND IT NEVER FIRED ON THE JOB IT LOOKED LIKE IT FIRED ON. On job b8f09ae9
    `cells_across_at_budget` is absent from the facts entirely - fitness.py sets it only when
    traits.narrow_vs_volume is truthy, and the measured traits of that file give None. The 3.91
    in its log is the conjugate planner's own sizing figure, which never reaches home_turf.
    """
    assert _turf(cells_across_at_budget=2.0) == "weak", "27% volume error, measured"
    assert _turf(cells_across_at_budget=3.0) == "weak", "32% volume error, measured"
    assert _turf(cells_across_at_budget=3.91) == "home", (
        "3.91 is above everything the row's own sentence measured, so it must not demote")
    assert _turf(cells_across_at_budget=4.0) == "home"
    row = next(r for r in SPEC.home_turf if r.fact == "cells_across_at_budget")
    assert (row.op, row.value) == ("<=", 3.0), (
        "the row has to stop where its evidence stops, and `<= 3.0` is what that is")


def test_the_other_multi_region_engine_declines_a_sealed_coolant_circuit():
    """THE MIRROR OF test_a_file_whose_fluid_is_a_body_is_outside, ON THE OTHER ENGINE.

    snappy_multiregion declared no coolant row at all, and `home_turf.verdict` returns home
    whenever no outside and no weak row holds - so it read HOME on a brazed plate whose coolant is
    sealed space inside the metal, a part it cannot mesh: its own advisories ask the user which of
    the solids is the fluid, and on a sealed circuit there is no fluid solid to name.

    On the Corintis plate the ranking still came out right, for a reason unrelated to the coolant:
    `closed` came back False off a welded tessellation and dragged it to weak. That is the row this
    file's own comments say answers the wrong question, so the right answer was resting on it.
    """
    other = get_spec("snappy_multiregion")
    sealed = {**CORINTIS, "closed": True}
    assert verdict(other, FLOW_MULTI_REGION, sealed).severity == "outside"
    assert verdict(SPEC, FLOW_MULTI_REGION, sealed).severity == "home"
    # and the mirror image: with the fluid already a body, the two swap
    body = {**sealed, "coolant_inside": False}
    assert verdict(other, FLOW_MULTI_REGION, body).severity == "home"
    assert verdict(SPEC, FLOW_MULTI_REGION, body).severity == "outside"
    # silent when nothing measured it, on both engines
    unknown = {k: v for k, v in sealed.items() if k != "coolant_inside"}
    assert verdict(other, FLOW_MULTI_REGION, unknown).severity == "home"
    assert verdict(SPEC, FLOW_MULTI_REGION, unknown).severity == "home"
    assert row_problems(other) == []
