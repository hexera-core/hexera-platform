"""The words a semiconductor thermal engineer types reach the cooling path, and nothing else moves.

Measured 2026-10-24. Before this the heat gate wanted a heat word, and a cold plate has none
("cold" is not one), so the plainest things a customer could type settled nothing:

    "mesh this cold plate"                      -> no cue, the domain question
    "150 W processor with a fan"                -> no cue, the domain question
    "die stack thermal"                         -> the thermal question
    "microchannel cold plate, water at 2 L/min" -> plain CFD, the metal never meshed

The scores on the shared phrase set, before the thermal-hardware cues and after:

    audit60       60/60  ->  60/60
    dev           45/45  ->  45/45
    holdout       44/45  ->  44/45       (the existing 150 did not move at all)
    semi_dev          -  ->  34/34       tuned on, so it proves only that nothing contradicts
    semi_holdout      -  ->  21/30       written before the first score, and 23/30 now

WHAT THE HOLDOUT CHANGED, so nobody has to reconstruct it. It scored 21/30 the first time it was
run, and nine misses were read rather than tuned away. Seven were ASKS - the reader could not
tell, so it asks its one question - and those are left exactly as they scored: an ask costs the
user one click, and tuning them away would spend the holdout for nothing. Two were defects, and
both are fixed in purpose_map:

  "solder joint reliability of a BGA"           read as cooling. Reliability analysis is
                                                mechanics; a wrong route costs the job.
  "stress from CTE mismatch in the die attach"  read as plain structural work. A CTE mismatch is
                                                differential thermal expansion, which is heat
                                                moving through a solid.

That is 23/30, with the seven asks the whole of what is left. Nothing in the set takes a wrong
path, which is the bar these tests hold it to rather than the score.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from purpose_phrases import SEMI_DEV, SEMI_HOLDOUT  # noqa: E402
from test_purpose_map import label_of, route_of  # noqa: E402

from meshpipeline.application import purpose_map as pm  # noqa: E402


# #
# what the cues were written for
# #
@pytest.mark.parametrize(("phrase", "want"), SEMI_DEV)
def test_every_phrase_the_cues_were_written_for(phrase, want):
    got = label_of(pm.read_purpose(phrase))
    assert got == want, f"{phrase!r} wanted {want}, got {got} ({pm.read_purpose(phrase).why})"


# #
# the set written before the first score, and scored once
# #
def test_the_holdout_keeps_the_score_it_was_given():
    exact = sum(label_of(pm.read_purpose(p)) == lab for p, lab in SEMI_HOLDOUT)
    assert exact >= 23, (
        "the semiconductor holdout scored 21/30 as written and 23/30 once the two defects it "
        "exposed were fixed; it may improve, but a drop means a cue was lost:\n"
        + "\n".join(f"   {p!r} wanted {lab}, got {label_of(pm.read_purpose(p))}"
                    for p, lab in SEMI_HOLDOUT if label_of(pm.read_purpose(p)) != lab))


def test_nothing_in_the_holdout_takes_a_wrong_path():
    """A miss is only ever the one question. Being asked costs a click; being sent to the wrong
    mesher costs the job, so that is the bar the holdout is held to rather than its score."""
    wrong = [(p, lab, label_of(pm.read_purpose(p))) for p, lab in SEMI_HOLDOUT
             if label_of(pm.read_purpose(p)) != "ask"
             and route_of(label_of(pm.read_purpose(p))) != route_of(lab)]
    assert wrong == [], wrong


# #
# and the nouns do not take over a sentence that is about something else
# #
@pytest.mark.parametrize("phrase", [
    "stress in this PCB", "drop test of this PCB", "modal analysis of this cold plate",
    "fatigue of the chip solder joints", "stress in this die cast housing",
    "can this cold plate hold 3 bar without bursting", "solder joint reliability of a BGA",
])
def test_a_cooling_noun_in_a_mechanics_question_is_still_mechanics(phrase):
    # The hardware noun is the weak evidence here, not the mechanics. A PCB in a drop test is a
    # board-level FEA job and always was; the thermal-hardware cues must not have taken it.
    assert pm.read_purpose(phrase).route == pm.ROUTE_FEA, pm.read_purpose(phrase).why


@pytest.mark.parametrize("phrase", [
    "drag on this car body", "external aero on a car at 30 m/s",
    "500 W pump, what is the flow rate", "pressure drop through this manifold",
    "mesh the water inside this pipe", "blood flow through this artery",
])
def test_the_flow_path_is_untouched(phrase):
    # Watts on a pump are not watts on a chip, and a manifold is not a cold plate.
    assert pm.read_purpose(phrase).route == pm.ROUTE_FLOW, pm.read_purpose(phrase).why


def test_the_cold_plate_is_the_case_this_exists_for():
    # Said four ways, because a customer will say it four ways.
    for phrase in ("mesh this cold plate", "cool this cold plate with water",
                   "microchannel cold plate, water at 2 L/min",
                   "conjugate heat transfer on this cold plate"):
        r = pm.read_purpose(phrase)
        assert r.route == pm.ROUTE_ELECTRONICS and r.ask == "", f"{phrase!r}: {r.why}"


def test_a_heat_word_is_no_longer_needed_for_heat_to_be_the_question():
    # The whole mechanism, stated once: hardware that is thermal by its nature opens the gate.
    assert not pm._HEAT.search("mesh this cold plate")
    assert pm._THERMAL_HARDWARE.search("mesh this cold plate")
    assert pm.read_purpose("mesh this cold plate").route == pm.ROUTE_ELECTRONICS
    # ...and it only opens it where the words name no mechanics
    assert pm._THERMAL_HARDWARE.search("stress in this cold plate")
    assert pm.read_purpose("stress in this cold plate").route == pm.ROUTE_FEA


def test_the_cue_words_are_each_said_once():
    # A chip is both cooling hardware and a cooling word; `why` is read by a person.
    why = pm.read_purpose("cool this chip").why
    assert why.count("chip") == 1, why
