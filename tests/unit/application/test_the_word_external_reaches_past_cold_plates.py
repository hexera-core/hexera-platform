# Responsibility: Hold the measured BREADTH of one kept decision - `external` read at the weaker
# strength (_WHERE_THE_AIR_IS) instead of as a veto over heat - so the next reader finds it scored
# rather than argued, and so it cannot be widened or narrowed again without a number moving.
# Boundaries: pure. It reads words through application/purpose_map and asserts on the reading. No
# store, no model, no geometry, and nothing here reads a source file for a string.
"""Taking `external` out of the _PAST_A_BODY veto is kept, and it reaches well beyond cold plates.

The decision is not in dispute: `thermal resistance` is in _THERMAL_HARDWARE because "external
thermal resistance" is how the trade states a cooling problem, and a veto that suppressed the heat
word in that very phrase was reading the wrong thing. What was unrecorded is how far it goes.

WHAT IT BUYS, MEASURED 2026-11-12 by putting `\\bexternal` back into the _PAST_A_BODY veto in
process and scoring the shared set both ways: 235/251 with it there, 242/251 with it where it is.
Seven rows, and five of them are the breadth itself - the other two are "cooling the brake disc
with external airflow" and "turbine blade cooling with external flow", both of which read
electronics on origin/main and flow with the word back in the veto.

WHAT IT COSTS, measured the same day over the 490 generated phrases swept below - 35 ordinary
parts, not one of them a semiconductor part, x 14 "external ..." frames - with HEAD 42d8a731's
purpose_map and this one loaded side by side as standalone modules. 291 of the 490 move:

    ask domain     ->  electronics  140
    ask domain     ->  ask thermal  110
    flow           ->  electronics   25
    fea            ->  ask domain     5
    flow external  ->  electronics    5
    fea            ->  ask thermal    4
    ask domain     ->  fea_heat       2

"external thermal resistance of the <part>" reaches electronics cooling for 34 of the 35 parts - a
car body, an engine block, a cylinder head, a bridge deck, a ship and a solar panel among them -
and six of those were flow on HEAD: the exhaust manifold, the duct, the nozzle, the mixing tank,
the wind turbine and the impeller. The one part it does not reach is the weldment, and that is
where all nine of the fea rows above are: `weld` is a mechanics cue, so HEAD read "external
thermal resistance of the weldment" as the FEA path outright - route fea, analysis thermal,
why="mechanics: weld" - and this file asks which physics was meant, which is what origin/main
does with it too.

AND THE COLD PLATE WAS NEVER WHAT THE DECISION WAS FOR. "external thermal resistance of this cold
plate" reads electronics on HEAD, here, AND with the word back in the veto, because a cold plate
is a coolant said outright and the coolant beats every veto in this module. A reading that cannot
move is no evidence about a change that moved 291 others.

The trade is an ASK for a THERMAL ROUTE, over and over - 110 of the 291 are one ask for another
and 140 are an ask answered - and not a wrong route for a right one. That is why it is kept, and
this file is the record of its size.

FOUR of the 490 are in the scored set and 486 are not, so the 242/251 says almost nothing about
any of this. The four are the first frame on the engine block, the car body, the duct and the
nozzle, which purpose_phrases carries as its `semi_breadth` origin.
"""
from __future__ import annotations

import sys
from pathlib import Path

from meshpipeline.application import purpose_map as pm

sys.path.insert(0, str(Path(__file__).resolve().parent))
from purpose_phrases import (  # noqa: E402  (a sibling data module, not an installed package)
    EXTERNAL_BREADTH_FRAMES,
    EXTERNAL_BREADTH_PARTS,
    sweep,
)

#: the one part of the 35 whose own name carries a mechanics cue, which is why it is the exception
#: to every count below
_WELDMENT = "weldment"


def _label(said: str) -> str:
    out = pm.read_purpose(said)
    if not out.settled:
        return "ask"
    return "fea_heat" if out.heat_in_solid else out.route


def test_the_sweep_is_the_one_the_counts_were_measured_on():
    said = sweep(EXTERNAL_BREADTH_PARTS, EXTERNAL_BREADTH_FRAMES)
    assert (len(EXTERNAL_BREADTH_PARTS), len(EXTERNAL_BREADTH_FRAMES)) == (35, 14)
    assert len(said) == 490
    assert len(set(said)) == 490, "a duplicate phrase would double-count"


def test_external_thermal_resistance_reaches_thirty_four_ordinary_parts():
    """The sharp end of the breadth, and the frame the decision was argued on. An engine block, a
    bridge deck and a ship are not cold plates, and this routes all three to electronics
    cooling."""
    frame = EXTERNAL_BREADTH_FRAMES[0]
    assert frame == "external thermal resistance of the {part}"
    reached = [p for p in EXTERNAL_BREADTH_PARTS if _label(frame.format(part=p)) == "electronics"]
    missed = [p for p in EXTERNAL_BREADTH_PARTS if p not in reached]
    assert len(reached) == 34, (len(reached), missed)
    assert missed == [_WELDMENT], missed
    for p in ("car body", "engine block", "cylinder head", "bridge deck", "ship", "solar panel"):
        assert _label(frame.format(part=p)) == "electronics", p


def test_the_one_part_it_does_not_reach_is_the_one_with_a_mechanics_word_in_its_name():
    """`weld` is a mechanics cue, so the sentence names both physics and the domain question is the
    answer - which is what origin/main does with it. Kept as the exception that shows the heat word
    is not simply winning everything."""
    out = pm.read_purpose("external thermal resistance of the weldment")
    assert not out.settled, out
    assert out.ask == pm.ASK_DOMAIN, out
    assert "weld" in out.why, out


def test_the_aerodynamic_quantity_still_wins_on_every_one_of_the_parts():
    """`external` did not become inert, it became weaker: an aerodynamic QUANTITY in the sentence
    is still flow past a body on 34 of the 35 parts, which is the half of _PAST_A_BODY that was
    kept. The weldment is the exception on all three frames for the same reason it is the
    exception above - `weld` names mechanics, so both physics are named and the domain question is
    the answer ("both: weld, drag")."""
    for p in EXTERNAL_BREADTH_PARTS:
        for frame in ("external drag on the {part}", "downforce on the {part}",
                      "wake behind the {part}"):
            said = frame.format(part=p)
            if p == _WELDMENT:
                assert not pm.read_purpose(said).settled, (said, pm.read_purpose(said))
                continue
            assert _label(said) == "flow", (said, pm.read_purpose(said))


def test_the_whole_sweep_has_the_distribution_that_was_recorded():
    """The size of the breadth, pinned. 170 of the 490 reach electronics cooling and 110 are still
    read as a flow; a change to either is a change to how far this decision goes, and it belongs
    in the module comment beside this number rather than in a silent diff."""
    said = sweep(EXTERNAL_BREADTH_PARTS, EXTERNAL_BREADTH_FRAMES)
    counts: dict[str, int] = {}
    for p in said:
        counts[_label(p)] = counts.get(_label(p), 0) + 1
    assert counts == {"electronics": 170, "ask": 205, "flow": 110, "fea_heat": 2, "fea": 3}, counts


def test_a_cold_plate_is_not_the_evidence_for_this_change():
    """The phrase the decision was argued on cannot move: a cold plate is a coolant said outright,
    and the coolant beats every veto here. MEASURED 2026-11-12 - electronics on origin/main's
    purpose_map, on HEAD 42d8a731's, here, and with `\\bexternal` put back into the veto in
    process. Asserted on the readings that CAN move instead: the same frame on an ordinary part,
    and the same aerodynamic quantity on a bare electronics noun. A cold plate keeps its coolant
    reading even under `drag`, which is how little this phrase can tell anyone."""
    assert _label("external thermal resistance of this cold plate") == "electronics"
    assert _label("external thermal resistance of the engine block") == "electronics"
    assert _label("external drag on this cold plate") == "electronics"
    assert _label("external drag on the chip enclosure") == "flow"


def test_almost_none_of_the_sweep_is_in_the_scored_set():
    """The 242/251 says almost nothing about any of these, which is the whole reason this file
    exists. FOUR of the 490 are in the scored set - the engine block, the car body, the duct and
    the nozzle on the first frame, carried as the set's `semi_breadth` origin - and the other 486
    are scored nowhere at all."""
    from purpose_phrases import PHRASES

    scored = {p for p, _lab, _w in PHRASES}
    said = set(sweep(EXTERNAL_BREADTH_PARTS, EXTERNAL_BREADTH_FRAMES))
    assert sorted(said & scored) == [
        "external thermal resistance of the car body",
        "external thermal resistance of the duct",
        "external thermal resistance of the engine block",
        "external thermal resistance of the nozzle"], sorted(said & scored)
    assert len(said - scored) == 486
    breadth = {p for p, _lab, w in PHRASES if w == "semi_breadth"}
    assert len(breadth) == 6
    # all six are the same FRAME, with "the"/"this" and ordinary parts: two of them ("this cold
    # plate", "the manifold") are not in the sweep's own 35 parts, which is why the overlap is 4
    assert all(p.startswith("external thermal resistance of") for p in breadth), breadth
