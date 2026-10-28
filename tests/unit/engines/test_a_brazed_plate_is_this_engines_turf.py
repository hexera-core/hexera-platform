# Responsibility: Verify the conjugate mesher's home turf is read off whether the coolant is the
# space INSIDE the part, not off how many solids the file holds.
# Boundaries: declarations and the turf verdict only; no geometry, no mesher, no model.
#
# WHY THIS FILE EXISTS. The first real customer part this engine was ever shown - a Corintis cold
# plate, six solids brazed into one piece with 52.14 ml of coolant cut out of it cleanly - put the
# engine OUTSIDE its own turf on `n_solids >= 2`, and the recommender offered the electronics snap
# grid for a STEP cold plate instead. A brazed or diffusion-bonded plate is a base and a lid, which
# is the commonest way one is made, and the engine's own confirmation wording had said so all along.
import pytest

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


def test_a_single_solid_is_home_even_before_the_split_has_run():
    """The older rule stands on its own: it needs no extraction to be true."""
    facts = {k: v for k, v in CORINTIS.items() if k != "coolant_inside"}
    assert verdict(SPEC, FLOW_MULTI_REGION, {**facts, "n_solids": 1}).severity == "home"


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
