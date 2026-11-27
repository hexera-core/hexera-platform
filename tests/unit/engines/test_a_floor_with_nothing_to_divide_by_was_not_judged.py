# Responsibility: Hold that this engine's layer tally counts the floors it EVALUATED, and that a
# pass whose log left every floor unjudged is not published as no layer pass at all.
# Boundaries: layers.floors_unjudged/shortfalls/measured and finalize._layers_published/_near_wall,
# over logs held in memory. Nothing here meshes anything, opens a mesh or draws a panel; the
# browser arms for the same two defects are in tests/ui.
"""A FLOOR IS JUDGED WHEN ITS TEST RAN, NOT WHEN ITS KEY WAS THERE.

Two readings of one zero-layer pass, both SYNTHETIC, measured 2026-11-17:

    layers.shortfalls      3 sentences - coverage 0.0000%, thickness 0.0%, 0.00 layers of 3
    layers.floors_unjudged ()          - so the panel's tally read "4 of the 4 floors this
                                         engine judges a layer pass by had a figure in the
                                         layer log", over a verdict on THREE

The fourth floor is `ILLEGAL_PER_MILLION_CEILING`, which is faces in error PER MILLION CELLS
ADDED, and `shortfalls` guards it on `int(cells) > 0`. This pass added no cells, so that
comparison never happened - and the panel printed "Faces in error 90,000" with no per-million
figure beside it for the very same reason, on the same page as the claim that four floors had
been judged. `LAYER_FLOORS`' own comment says it takes both keys precisely because `shortfalls`
skips that one; what it did not do was require the denominator to be a number you can divide by.

AND THE SECOND DEFECT IS THE OPPOSITE SHAPE: a reading that was published as an absence. An
unreadable layer log and NO LAYER PASS AT ALL both came out of `finalize._layers_published` as
`{}`, so the two payloads were equal key for key and the two panels were identical. Neither reads
as a clean pass, so that was a completeness gap and never a false claim - but "we grew layers and
cannot tell you how they came out" and "we grew none" call for different actions, and a reader
could not tell which they were looking at.
"""
from __future__ import annotations

import pytest
from test_a_layer_pass_on_the_native_path_is_published import GOOD_LOG, REAL_LOG

from meshpipeline.engines.cfmesh_conjugate import finalize as F
from meshpipeline.engines.cfmesh_conjugate import layers as LY
from meshpipeline.engines.cfmesh_conjugate.conjugate import FLUID_TO_SOLID

#: A PASS THAT GREW NOTHING, SYNTHETIC, in the shape `layers.complain`'s own docstring describes:
#: with OpenFOAM's shipped `minVol 1e-13` every face of a geometrically correct microchannel mesh
#: is flagged on length scale alone, nothing is extruded, and the run still exits 0. The table row
#: is the plate's interface with its three figures taken to zero, and the illegal-face count is
#: the count of the mesh the pass was HANDED - 90,000 faces in error over no cells added at all,
#: which is the one floor here that is a ratio and the one that therefore cannot be evaluated.
ZERO_LAYER_LOG = """
Extruding 0 out of 16582198 faces (0%). Removed extrusion at 16582198 faces.
Added 0 out of 0 cells (0%).

patch          faces        layers        overall thickness
                        target   mesh     [m]       [%]
-----          -----    -----    ----     ---       ---
fluid_to_solid 16582198 3        0        0         0

Layers added in = 12.3 s.
Finished meshing with 90000 illegal faces (concave, zero area or negative cell pyramid volume)
"""


def _grown(log: str) -> dict:
    return LY.measured(LY.read_layer_log(log), interface_patch=FLUID_TO_SOLID)


def _published(log: str, *, asked: int = 3) -> dict:
    """The layers block exactly as `conjugate.build` writes it and `finalize` publishes it: the
    log read, then the shortfalls computed against the pass's OWN `cells_added`, then the block."""
    raw = LY.read_layer_log(log)
    cells = int(raw.get("cells_added") or 0)
    short = list(LY.shortfalls(raw, interface_patch=FLUID_TO_SOLID, n_layers=asked, cells=cells))
    if short:                             # exactly as conjugate.build writes it: only when non-empty
        raw["shortfalls"] = short
    return F._layers_published(raw)


# ----------------------------------------------- the floor whose test never ran (SYNTHETIC)

def test_the_faces_in_error_floor_is_not_judged_when_no_cells_were_added():
    """THE DEFECT. `shortfalls` skipped this floor on `int(cells) > 0` and `floors_unjudged` said
    every floor had been judged, because 0 is not None."""
    got = _grown(ZERO_LAYER_LOG)
    assert got["layer_faces_in_error"] == 90000, "the premise: the count itself WAS read"
    assert got["layer_cells_added"] == 0, "the premise: and there is nothing to divide it by"
    assert "faces in error" in LY.floors_unjudged(got), (
        "a floor measured as faces in error PER MILLION CELLS ADDED, on a pass that added no "
        f"cells, is counted as one this engine judged: {LY.floors_unjudged(got)!r}")


def test_the_tally_counts_the_floors_that_were_judged_and_not_the_keys_that_were_present():
    """The number the panel prints as its denominator's numerator. Three of the four floors
    produced a verdict on this pass and the fourth could not, so the tally is three."""
    pub = _published(ZERO_LAYER_LOG)
    judged = pub["layer_floors_total"] - len(pub["layer_floors_unjudged"])
    # TWO, not three: the floor that replaced the coverage one is drawn on
    # `layer_kept_of_offered_pct`, which an all-zero log cannot produce, so it joins
    # "faces in error" as a floor nothing could judge. See layers.KEPT_OF_OFFERED_FLOOR_PCT.
    assert judged == 2, (
        "the payload says this pass was judged by "
        f"{judged} of {pub['layer_floors_total']} floors, and only three of its tests ran: "
        f"{pub['layer_floors_unjudged']!r}")
    assert not any("per million" in s for s in pub["layer_short"]), (
        "the premise of that count: the faces-in-error floor produced no sentence either way, "
        f"which is what being unjudged means here: {pub['layer_short']!r}")


def test_the_three_floors_a_zero_layer_pass_misses_at_zero_are_still_judged_at_zero():
    """THE BROADENING THIS IS NOT. 0.0000% coverage, 0.0% thickness and 0.00 layers kept are the
    largest misses those three floors can have, and a rule that read every zero as "nothing to
    judge" would have silenced the three real misses to close the fourth. Only a DENOMINATOR is
    treated this way."""
    pub = _published(ZERO_LAYER_LOG)
    assert pub["layer_floors_unjudged"] == ["faces kept of those offered",
                                            "faces in error"], (
        f"a floor that was judged at zero is reported as unjudged: {pub['layer_floors_unjudged']!r}")
    # TWO. The coverage sentence is gone with the coverage floor: a pass offered 0.0000% and
    # keeping all of it is not a mover failing, which is what the floor that replaced it
    # measures. "nothing grew at all" is `layers.complain`'s job, not a floor's.
    assert len(pub["layer_short"]) == 2, pub["layer_short"]
    assert not any("of the interface against" in s for s in pub["layer_short"])
    assert any("0.0% of the thickness" in s for s in pub["layer_short"]), pub["layer_short"]
    assert any("kept 0.00 layers of the 3" in s for s in pub["layer_short"]), pub["layer_short"]


def test_a_cell_count_that_is_not_a_number_is_not_a_denominator():
    """`measured()`'s dict is built from JSON written on the far side of a container boundary, and
    `True` would pass a bare `> 0` test as a cell count of one."""
    for junk in (True, "37247044", None, 0.0, -1):
        got = {"layer_coverage_pct": 95.0, "layer_thickness_pct": 95.0, "layers_in_mesh": 2.8,
               "layer_faces_in_error": 2888, "layer_cells_added": junk}
        assert "faces in error" in LY.floors_unjudged(got), (
            f"{junk!r} was accepted as the cell count this floor's ratio is taken over: "
            f"{LY.floors_unjudged(got)!r}")


def test_a_pass_that_added_cells_still_has_that_floor_judged():
    """The other side of the same test, and the one that must not move: REAL PLATE (job 0ff6f9e5)
    added 37,247,044 cells and left 8,916 faces in error, which IS a ratio - 239 per million."""
    got = _grown(REAL_LOG)
    assert got["layer_cells_added"] == 37_247_044 and got["layer_faces_in_error"] == 8916
    assert LY.floors_unjudged(got) == (), (
        f"the real plate's four judged floors stopped being four: {LY.floors_unjudged(got)!r}")
    assert LY.floors_unjudged(_grown(GOOD_LOG)) == (), (
        "and neither did the measured run's, which met every one of them")


# ----------------------------------------------- an unreadable log is not the absence of a pass

def test_an_unreadable_layer_log_and_no_layer_pass_no_longer_publish_the_same_payload():
    """THE DEFECT, measured as the thing a reader gets. SYNTHETIC: `read_layer_log("")` is what
    `conjugate.build` hands over when the layer log is missing or unparseable, and `{}` is what it
    hands over when `layers` was 0 and no pass ran at all."""
    unreadable = F._layers_published(LY.read_layer_log(""))
    no_pass = F._layers_published({})
    assert unreadable != no_pass, (
        "a run whose layer log could not be read publishes the same thing as a run that never "
        f"grew a layer: {unreadable!r}")
    assert no_pass == {}, (
        f"and a run with no layer pass at all stopped being silent: {no_pass!r}")


def test_the_unreadable_case_says_which_floors_it_could_not_judge_and_claims_nothing_else():
    names = [n for n, _ in LY.LAYER_FLOORS]
    unreadable = F._layers_published(LY.read_layer_log(""))
    assert unreadable.get("layer_floors_unjudged") == names, (
        "an unreadable log has to name every floor it left unjudged, which is all of them, and "
        f"this payload says: {unreadable.get('layer_floors_unjudged')!r}")
    assert unreadable.get("layer_floors_total") == len(LY.LAYER_FLOORS), (
        "and how many floors there are to be unjudged against: "
        f"{unreadable.get('layer_floors_total')!r}")
    assert "layer_short" not in unreadable, (
        "an empty shortfall list is the one value a reader is entitled to read as a clean pass, "
        f"and this run was never judged: {unreadable!r}")
    assert "layer_iterations" not in unreadable, (
        "the iteration count is 0 on every unreadable log and says only that nothing was counted, "
        f"so publishing it puts a figure on a pass nothing measured: {unreadable!r}")


def test_the_two_panels_a_reader_would_see_are_no_longer_equal_key_for_key():
    """The payload the panel is drawn from is `_layers_published` plus `_near_wall`, and BOTH of
    those used to come out identical: `near_wall_layers` 3 with `near_wall_layers_measured` False,
    which is the ask standing in for a measurement on either run. SYNTHETIC plan."""
    plan = {"background_cell_m": 2.0e-04, "levels": 3, "layers": 3}
    unreadable = F._layers_published(LY.read_layer_log(""))
    no_pass = F._layers_published({})
    a = {**unreadable, **F._near_wall(plan, unreadable)}
    b = {**no_pass, **F._near_wall(plan, no_pass)}
    assert a != b, f"the two payloads are still equal key for key: {a!r}"
    # and the half that was already right does not move: the count on the row is the ASK on both
    for payload in (a, b):
        assert payload["near_wall_layers"] == 3 and payload["near_wall_layers_measured"] is False


def test_a_pass_whose_log_was_read_in_part_is_unchanged():
    """The state between the two, which this change must not disturb: coverage was read and clears
    its floor, so `layer_short` is `[]` - a judgement - and three floors are named as unjudged.
    SYNTHETIC: the plate's first extrusion line and its cell count with the rest cut off."""
    truncated = ("Extruding 16122330 out of 16582198 faces (97.2267%). "
                 "Removed extrusion at 334828 faces.\n"
                 "Added 49746594 out of 49746594 cells (100%).\n")
    pub = _published(truncated)
    assert pub["layer_short"] == [] and pub["layer_floors_total"] == 4
    assert pub["layer_floors_unjudged"] == ["thickness", "layers kept", "faces in error"], (
        f"the partly-read case moved: {pub['layer_floors_unjudged']!r}")
    assert pub["layer_cells_added"] == 49_746_594, (
        "and its cell count is positive, which is why 'faces in error' is unjudged here for the "
        "OTHER reason - no illegal-face count was read at all")


# ----------------------------------------------- the record that must not regress

def test_the_real_plates_published_layer_block_is_unchanged():
    """REAL PLATE (job 0ff6f9e5), read from that run's own layer log. Neither change touches it:
    four floors judged, three missed, and every figure the user's record quotes."""
    pub = _published(REAL_LOG)
    assert pub["layer_floors_unjudged"] == [] and pub["layer_floors_total"] == 4
    assert len(pub["layer_short"]) == 3, pub["layer_short"]
    assert pub["layers_in_mesh"] == pytest.approx(2.25) and pub["layers_asked"] == 3
    assert pub["layer_coverage_pct"] == pytest.approx(88.5826)
    assert pub["layer_thickness_pct"] == pytest.approx(71.9)
    assert pub["layer_faces_in_error"] == 8916 and pub["layer_cells_added"] == 37_247_044
    assert pub["layer_iterations"] == 10
    assert any("239 per million" in s for s in pub["layer_short"]), pub["layer_short"]


def test_the_layers_floor_is_still_met_at_exact_equality_on_the_real_plate():
    """THE JUDGEMENT CALL, decided and not inherited. The test is a strict `<` against
    `LAYERS_FLOOR_FRACTION * asked`, and the plate kept 2.25 of 3 where that product is 2.25
    EXACTLY - so the plate meets this floor and its record says three floors missed, not four.

    STRICT IS RIGHT, for two reasons that are measurements and not taste. The floor reads "at
    least 75% of the ask", so a `<=` would report a shortfall against a bar the pass reached. And
    the equality cannot fall the other way on another host: 0.75 is 3/4 and 2.25 is 9/4, both
    exactly representable in binary. It is also the same convention as this module's other three
    floors - coverage and thickness are missed on `<` and the error ratio on `>` - so a pass
    sitting exactly on any of the four bars meets it.
    """
    assert LY.LAYERS_FLOOR_FRACTION * 3 == 2.25, "the boundary this case sits on is not exact"
    pub = _published(REAL_LOG)
    assert not any("layers of the 3" in s for s in pub["layer_short"]), (
        f"2.25 of 3 is reported as falling under a floor of 2.25: {pub['layer_short']!r}")
    assert pub["layers_in_mesh"] == 2.25
    # and one hundredth of a layer under it is a shortfall, so the boundary is where it is claimed
    under = _published(REAL_LOG.replace("3        2.25", "3        2.24"))
    assert any("kept 2.24 layers of the 3" in s for s in under["layer_short"]), under["layer_short"]
