# Responsibility: Hold the ordinary CFD, FEA and thermal readings the semiconductor cues broke, one
# assertion per phrase, and hold the cue-set rules that fixed them - including the two cue-set
# MEMBERSHIPS that moved readings while every test in this file stayed green.
# Boundaries: pure. It reads words through application/purpose_map and asserts on the reading; no
# store, no model, no geometry.
"""Growing the reader for cold plates cost eleven readings that already worked.

MEASURED 2026-11-12, origin/main's purpose_map and HEAD 42d8a731's loaded side by side as
standalone modules: these eleven are where the branch was wrong and main was right. None of the
eleven was in tests/unit/application/purpose_phrases.py, which is why 221/230 stayed green while
ordinary external aero asked "no cue" on an aerofoil. Every row is re-derivable one phrase at a
time - origin/main's reading first, then HEAD 42d8a731's, which is the one each fix below undoes.
This file is green only when the reading is origin/main's again, so the right-hand column is what
this tree must NOT say:

    mesh this aerofoil                              flow external  ->  ask domain, why "no cue"
    aeroacoustics of the side mirror                flow external  ->  ask domain, why "no cue"
    wind tunnel test of a heat sink                 electronics    ->  flow external
    heat sink in a wind tunnel at 5 m/s             electronics    ->  flow external
    cooling the brake disc with external airflow    electronics    ->  flow external
    turbine blade cooling with external flow        electronics    ->  flow external
    inverter duct flow                              flow internal  ->  electronics
    mesh this wafer check valve                     ask domain     ->  electronics
    chip ejection chute                             ask domain     ->  electronics
    processor of the signal, mesh the housing       ask domain     ->  electronics
    mesh this interposer plate                      ask domain     ->  electronics

THREE CAUSES, each in one cue set:

  1. `\\baero` was anchored to `\\baero\\b` so that AEROSPACE would stop voting for flow. The
     anchor was right - "aerospace bracket, static stress" had been reading as mechanics AND flow
     - but the prefix had also been carrying the aero words that are not the word "aero".
  2. The _PAST_A_BODY veto held `external` and `wind`. Those say WHERE THE AIR IS, not what is
     being asked about it, and that is exactly how a user says a part is air-cooled. The four
     thermal rows above are four pieces of genuinely air-cooled work sent to car-style CFD, where
     the metal is never meshed at all.
  3. A _THERMAL_HARDWARE noun opened the heat gate with no other evidence in the sentence, so the
     noun beat an explicit flow cue: "inverter duct flow" says outright what the user wants.

AND THE FIX FOR 2 AND 3 TOGETHER OPENED A FOURTH HOLE, which the review of it caught by running the
same comparison on the enclosure sentences a customer actually writes. MEASURED 2026-11-12, HEAD
42d8a731's purpose_map and the half-fixed tree loaded side by side as standalone modules - HEAD's
reading first, then the tree with both words out of EVERY veto, which is not what it holds now
(origin/main, HEAD and this tree all read these four as flow/external, asserted below):

    wind tunnel test of this electronics enclosure          flow external  ->  electronics
    wind tunnel test of an electronics enclosure at 20 m/s  flow external  ->  electronics
    wind pressure on the electronics enclosure              flow external  ->  electronics
    external flow over the inverter cabinet with its fans   flow external  ->  electronics

The cause was joint: `external` and `wind` had left the veto, and the new corroboration set accepts
`enclosure` and `fans?`, so a bare electronics noun opened the heat gate with nothing left to stop
it. The wind loading of an outdoor electronics cabinet went down the conjugate-cooling path, and
the last of the four says "external flow" outright. Fixed by giving the two words back at the
weaker strength they belong at - a veto on the bare hardware noun only, never on a heat word -
which is application/purpose_map's _WHERE_THE_AIR_IS.

AND THEN THE SAME MISTAKE TWICE MORE, both of them a cue-set MEMBERSHIP that moved readings with
nothing asserting on it. `aerofoil` and `aeroacoustic` were added to the _PAST_A_BODY veto as well
as to _FLOW, and _PAST_A_BODY is a veto over HEAT: a turbine blade is an aerofoil whose whole job
is heat. MEASURED 2026-11-12 over 192 generated turbomachinery and acoustics phrases
(purpose_phrases.TURBO_BODIES x TURBO_FRAMES, swept below) through four purpose_maps - origin/main,
HEAD 42d8a731, with the two patterns in the veto, with them out - 96 of the 192 changed, in all 96
the reading with them OUT matched BOTH baselines, and in none of the 96 did the reading with them
in match either. Both baselines first, then the reading with them in the veto:

    conduction through the turbine blade aerofoil           fea_heat     ->  flow
    internal cooling of the aerofoil                        electronics  ->  flow
    cooling the nozzle guide vane aerofoil with film holes   electronics  ->  flow
    conduction through the aeroacoustic liner               fea_heat     ->  flow
    thermal stress in the compressor aerofoil               fea_heat     ->  fea

Four of those are a wrong ROUTE and not an ask, the fifth loses the heat in the solid, and the
customer is a gas-turbine one. The removal cost nothing (242/251 both ways, same nine misses) and
keeping them bought nothing (the four phrases they were added for read flow with them in AND out,
because _FLOW already carries them). And the OTHER membership, `\\baerofoil` in _FLOW, created a
mechanics tie HEAD never had: 24 FEA phrases about a British-spelt aerofoil went from fea to "ask
domain". Both are asserted below, on readings, one phrase per assertion.

AND "wind tunnel test of this enclosure" IS NOT THE PHRASE THAT PROVES ANY OF THIS, and neither
is "stress in this airfoil". Each reads the same way on HEAD and here whatever the cue sets hold -
a bare enclosure is not in _THERMAL_HARDWARE, and the American spelling matches no flow cue at
all - and each is what this file pinned instead of the one word over that does move. Both are
kept below as controls beside the variants that fail.

THE FOUR FALSE FRIENDS are asked about rather than guessed, which is the choice this file records.
A wafer check valve is a valve, a chip ejection chute carries swarf, "processor of the signal" is a
verb and an interposer plate is a plate - the same shape as "mesh this part for Tim" matching TIM.
Being asked costs one click; all four would otherwise have been routed to a conjugate mesher.

AND NOTHING SEMICONDUCTOR MOVED. The shared phrase set scored 221/230 before any of these fixes
and 221/230 after all of them, with the same nine misses; the fifteen rows are the set's
`semi_regress` origin and six more are its `semi_breadth` origin, which take the whole set to 251
phrases and 242/251.
"""
from __future__ import annotations

import pytest

from meshpipeline.application import purpose_map as pm


def _label(said: str) -> str:
    out = pm.read_purpose(said)
    if not out.settled:
        return "ask"
    return "fea_heat" if out.heat_in_solid else out.route


# #
# 1. an aero word that is not the word "aero"
# #
@pytest.mark.parametrize("said", ["mesh this aerofoil", "aeroacoustics of the side mirror",
                                  "aerofoil at 2 degrees incidence",
                                  "aeroacoustic noise from the wing"])
def test_an_aerofoil_is_a_flow_past_a_body(said):
    out = pm.read_purpose(said)
    assert out.route == pm.ROUTE_FLOW, out
    assert out.topology == "external", out
    assert out.why != "no cue", "the reason denied a cue on a sentence whose only noun is an aerofoil"


MECHANICS_FRAMES = ("stress in this {b}", "vibration of this {b}", "modal analysis of this {b}",
                    "FEA of this {b}", "is this {b} strong enough", "fatigue of this {b}",
                    "buckling of this {b}", "deflection of this {b}", "drop test of this {b}",
                    "von Mises stress in this {b}", "natural frequencies of this {b}",
                    "how much load can this {b} take")


@pytest.mark.parametrize("said", [f.format(b=b) for b in ("aerofoil", "aeroacoustic liner")
                                  for f in MECHANICS_FRAMES])
def test_a_stress_analysis_of_an_aerofoil_is_mechanics(said):
    """THE SPELLING THAT CAN ACTUALLY FAIL. This file's only mechanics assertion used to be
    `_label("stress in this airfoil") == "fea"` - the AMERICAN spelling, which the module's own
    comment says was deliberately never added to _FLOW. That assertion cannot fail: `airfoil`
    matches no flow cue on origin/main, on HEAD 42d8a731 or here, so it reads as mechanics
    whatever this branch does. It was the enclosure mistake one spelling over.

    The BRITISH spelling is the one the branch added, and it is what moved. MEASURED 2026-11-12
    with HEAD 42d8a731's purpose_map and this one loaded side by side as standalone modules, over
    these twelve frames x two bodies - 24 phrases, every one of them "fea" on HEAD and "ask
    domain" here before the fix, with why="both: stress, aerofoil". A stress analysis of an
    aerofoil is FEA, so it reads as FEA again: where the WHOLE of the flow evidence is the body's
    name, the mechanics decides (application/purpose_map._A_BODYS_NAME). The route, the analysis
    type and the reason string are HEAD's, on all 24.
    """
    out = pm.read_purpose(said)
    assert _label(said) == "fea", out
    assert out.why.startswith("mechanics: "), out
    assert "aerofoil" not in out.why and "aeroacoustic" not in out.why, out


def test_one_aerodynamic_quantity_and_the_tie_stands():
    """The rule is about the body's NAME being the whole of the flow evidence, not about aerofoils
    being exempt. Add a word that asks for an aerodynamic quantity and both physics are named
    again, so the domain question is the right answer."""
    for said in ("drag on this aerofoil and its stress", "stress and the lift of this aerofoil",
                 "wake behind this aerofoil, and is it strong enough"):
        out = pm.read_purpose(said)
        assert not out.settled, out
        assert out.ask == pm.ASK_DOMAIN, out


def test_the_american_spelling_is_left_exactly_where_main_had_it():
    """`\\baero` never matched "airfoil", so adding it would change a reading rather than restore
    one: MEASURED 2026-11-12, "mesh this airfoil" asks the domain question on origin/main, on
    HEAD 42d8a731 and here. Kept as the CONTROL for the test above - the same twelve frames on
    the spelling that is inert, beside the spelling that moved."""
    assert not pm.read_purpose("mesh this airfoil").settled
    for frame in MECHANICS_FRAMES:
        assert _label(frame.format(b="airfoil")) == "fea", frame


# #
# 1b. ...and a body's name is not a veto over heat: a turbine blade IS an aerofoil
# #
@pytest.mark.parametrize("said,wanted", [
    ("conduction through the turbine blade aerofoil", "fea_heat"),
    ("internal cooling of the aerofoil", "electronics"),
    ("cooling the nozzle guide vane aerofoil with film holes", "electronics"),
    ("conduction through the aeroacoustic liner", "fea_heat"),
    ("thermal stress in the compressor aerofoil", "fea_heat"),
])
def test_an_aerofoil_that_is_a_heat_job_is_read_as_one(said, wanted):
    """`aerofoil` and `aeroacoustic` were put in the _PAST_A_BODY veto as well as in _FLOW, on the
    argument that each names a body in a free stream. But _PAST_A_BODY is a VETO OVER HEAT, and a
    turbine blade is an aerofoil whose whole job is heat: internal cooling, film cooling, and the
    conduction through the metal between the two.

    MEASURED 2026-11-12 on the 192 generated turbomachinery and acoustics phrases of
    purpose_phrases.TURBO_BODIES x TURBO_FRAMES, through four purpose_maps - origin/main, HEAD
    42d8a731, the two patterns in the veto, and the two patterns out of it. 96 of the 192 change,
    and in ALL 96 the reading with them OUT matches both origin/main AND HEAD while the reading
    with them in matches NEITHER; 94 of the 96 are a settled route under the veto rather than an
    ask. Four of these five were a WRONG ROUTE and not an ask, which is the cost this file's own
    doctrine says must never be paid, and the fifth keeps the FEA path but loses the heat in the
    solid - and the customer is a gas-turbine one, not a semiconductor one.

    THE REMOVAL COST NOTHING: 242/251 on the shared set with them in and 242/251 with them out,
    the same nine misses either way. Nothing consulted this membership before, which is why
    96 of these readings could change with every test in this file still green.
    """
    assert _label(said) == wanted, pm.read_purpose(said)


def test_the_two_body_names_buy_nothing_in_the_veto():
    """The benefit of keeping them was measured at zero too: the four phrases they were added for
    read flow/external on origin/main, with them in the veto AND with them out, because
    `\\baerofoil` and `\\baeroacoustic` in _FLOW already carry them. Asserted on readings of
    sentences one word apart, not on the sets, which are private.

    The pair is: the same body name with a heat word (the veto would have fired) and without one
    (it never could). Both must read as the physics the sentence names."""
    assert _label("internal cooling of the aerofoil") == "electronics"
    assert _label("mesh this aerofoil") == "flow"
    assert _label("conduction through the aeroacoustic liner") == "fea_heat"
    assert _label("aeroacoustic noise from the wing") == "flow"
    # ...and the branch's own corpus row, which read flow while the patterns were in the veto
    assert _label("turbine blade cooling with external flow") == "electronics"
    assert _label("turbine blade aerofoil cooling") == "electronics"


#: The frames of the 192-phrase sweep whose sentence is ABOUT HEAT and cannot be read any other
#: way - cooling it, the coolant's passages, the conduction through it, the thermal stress in it.
#: The sweep's other nine frames are left out on purpose: "drag on the ...", "... at 2 degrees
#: incidence" and "noise from the ..." are genuinely flow, "mesh the ..." names only the body, and
#: "temperature inside the ...", "heat transfer in the ...", "metal temperature of the ..." and
#: "thermal analysis of the ..." read as a heat question in a flow, which is what origin/main does
#: with them and is not what this membership was about.
_HEAT_FRAMES = ("conduction through the {part}", "internal cooling of the {part}",
                "cooling the {part} with film holes", "thermal stress in the {part}",
                "cooling passages in the {part}", "the {part} with its cooling channels",
                "conjugate heat transfer in the {part}")


def test_no_cooled_aerofoil_in_the_whole_sweep_is_vetoed_into_a_flow():
    """THE MEMBERSHIP, SWEPT - 84 phrases, not five.

    The five rows above are the worst cases; this is the rule they are cases of. Every one of
    these sentences is about heat in a body whose name happens to be a flow cue, and with
    `aerofoil` and `aeroacoustic` in the _PAST_A_BODY veto every one of them was read as flow past
    a body instead - the whole metal never meshed. MEASURED 2026-11-12: 96 of the 192 moved, 94 of
    them to a settled route, and this is the 84 of them that cannot be anything but heat.

    Swept from purpose_phrases so the lists the recorded counts were measured on are the lists
    this executes, and asserted on the READING, never on the cue set, which is private.
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from purpose_phrases import TURBO_BODIES, sweep

    said = sweep(TURBO_BODIES, _HEAT_FRAMES)
    assert len(said) == 84, len(said)
    flowed = [(p, pm.read_purpose(p).why) for p in said if _label(p) == "flow"]
    assert flowed == [], flowed
    # ...and it is not passing because nothing settles: every one is a heat path or an ask, and
    # most are a heat path.
    assert all(_label(p) in ("electronics", "fea_heat", "ask") for p in said)
    assert sum(_label(p) in ("electronics", "fea_heat") for p in said) == 80


def test_aerospace_is_still_not_aerodynamics():
    """The anchor these words were lost to is the point of the anchor, and it stays."""
    assert _label("aerospace bracket, static stress") == "fea"
    assert "aero" not in pm.read_purpose("aerospace bracket, static stress").why
    assert _label("aerospace power module cold plate, coolant at 2 L/min") == "electronics"


# #
# 2. air moved AT a piece of thermal hardware is cooling, not drag
# #
@pytest.mark.parametrize("said", [
    "wind tunnel test of a heat sink",
    "heat sink in a wind tunnel at 5 m/s",
    "cooling the brake disc with external airflow",
    "turbine blade cooling with external flow",
])
def test_air_cooled_thermal_work_reaches_the_cooling_path(said):
    assert _label(said) == "electronics", pm.read_purpose(said)


@pytest.mark.parametrize("said", [
    "external drag on an electronics enclosure",   # drag: the quantity asked for
    "wake behind the chip conveyor",               # wake
    "downforce on this cooling vent",              # downforce
    "aero of a radiator duct",                     # aero
    "drag on this car body",
])
def test_the_words_that_really_do_mean_flow_past_a_body_still_win(said):
    assert _label(said) == "flow", pm.read_purpose(said)


@pytest.mark.parametrize("said", [
    "wind tunnel test of this electronics enclosure",
    "wind tunnel test of an electronics enclosure at 20 m/s",
    "wind pressure on the electronics enclosure",
    "external flow over the inverter cabinet with its fans",
])
def test_the_wind_loading_of_an_electronics_cabinet_is_not_a_cooling_job(said):
    """The four the review caught. MEASURED 2026-11-12 against HEAD 42d8a731: every one of these
    is flow/external on HEAD and was electronics here, because `enclosure` and `fans?` are enough
    corroboration for a bare electronics noun to open the heat gate and `external` and `wind` had
    left the veto. The last says "external flow" outright and was overridden anyway."""
    out = pm.read_purpose(said)
    assert out.route == pm.ROUTE_FLOW, out
    assert out.topology == "external", out


def test_a_bare_enclosure_is_the_control_and_not_the_proof():
    """The phrase this file used to pin as the evidence that dropping `wind` was safe was
    "wind tunnel test of this enclosure". MEASURED 2026-11-12: it reads flow on HEAD 42d8a731, flow with the
    two words out of every veto, and flow with them back in at the weaker strength - a bare
    enclosure is not in _THERMAL_HARDWARE, so the gate cannot open on it and no veto is ever
    consulted. One word apart is where the reading actually turns, and that word is `electronics`,
    which is a _THERMAL_HARDWARE noun."""
    assert _label("wind tunnel test of this enclosure") == "flow"
    assert _label("wind tunnel test of this electronics enclosure") == "flow"
    # ...and the same noun with no where-the-air-is word in the sentence does reach cooling, so
    # the control above is not passing because `electronics` is inert here.
    assert _label("airflow over this electronics enclosure") == "electronics"


def test_where_the_air_is_loses_to_a_heat_word_and_beats_a_bare_hardware_noun():
    """Two vetoes at two strengths: `drag`, `wake` and `downforce` beat a heat word, `external`
    and `wind` do not and beat only a hardware noun that has no heat word behind it.

    Asserted on the READING of sentences that differ in one word, because the cue sets are private
    and a test of their membership would pass on a set nothing consults. Same part, same heat
    word, and only the aerodynamic word changes."""
    assert _label("external airflow over this heat sink") == "electronics"
    assert _label("wind over this heat sink") == "electronics"
    assert _label("drag on this heat sink") == "flow"
    assert _label("wake behind this heat sink") == "flow"
    assert _label("downforce on this heat sink") == "flow"
    # no heat word in the sentence at all, only a bare electronics noun with air moved at it:
    # one word decides, and it is the word that says where the air is.
    assert _label("airflow over this electronics enclosure") == "electronics"
    assert _label("external airflow over this electronics enclosure") == "flow"


# #
# 3. a bare electronics noun is not a question about heat
# #
def test_an_explicit_flow_cue_outranks_a_bare_electronics_noun():
    out = pm.read_purpose("inverter duct flow")
    assert out.route == pm.ROUTE_FLOW, out
    assert out.topology == "internal", out


@pytest.mark.parametrize("said", [
    "mesh this wafer check valve",
    "chip ejection chute",
    "processor of the signal, mesh the housing",
    "mesh this interposer plate",
])
def test_a_false_friend_is_asked_about_rather_than_routed(said):
    out = pm.read_purpose(said)
    assert not out.settled, f"{said!r} was routed to {out.route} on a homonym ({out.why})"
    assert out.ask == pm.ASK_DOMAIN, out


@pytest.mark.parametrize("said", [
    # the noun names heat, coolant or silicon outright: it speaks for itself
    "mesh this cold plate", "pin fin array in this cold plate", "vapour chamber under the GPU",
    "jet impingement on a bare die", "TIM2 bondline thickness effect", "hotspot at 500 W/cm2",
    "what is the junction to ambient resistance",
    # ...or something in the sentence is cooling it
    "150 W processor with a fan", "airflow over the PCB", "airflow around the GPU",
    "fan blowing over the processor", "air over the IGBT module", "air around the chip package",
])
def test_a_hardware_noun_with_evidence_behind_it_still_reaches_cooling(said):
    assert _label(said) == "electronics", pm.read_purpose(said)


def test_a_mechanics_question_about_the_same_nouns_is_still_mechanics():
    """The older rule, unchanged: the hardware noun is the weak evidence, not the mechanics."""
    for said in ("stress in this PCB", "drop test of this PCB",
                 "modal analysis of this cold plate", "fatigue of the chip solder joints"):
        assert _label(said) == "fea", pm.read_purpose(said)


# #
# and the whole set, so the eleven cannot be fixed by losing something else
# #
def test_the_whole_set_keeps_every_new_row_too():
    """The breadth rows and the regression rows are scored, not just recorded: 242/251, and no row
    of either new origin may be a miss. A phrase added to document a decision that then fails the
    reader is a decision nobody checked."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from purpose_phrases import ADDED_AFTER_221, PHRASES

    new = [(p, lab, w) for p, lab, w in PHRASES if w in ADDED_AFTER_221]
    assert len(new) == 21, f"the new origins hold {len(new)} rows"
    assert [f"{p!r} wanted {lab}, got {_label(p)}" for p, lab, _w in new if _label(p) != lab] == []
    exact = sum(_label(p) == lab for p, lab, _w in PHRASES)
    assert (exact, len(PHRASES)) == (242, 251), f"{exact}/{len(PHRASES)}"


def test_the_shared_phrase_set_keeps_the_score_it_had():
    """221/230 before these fixes and 221/230 after, with the same nine misses. Scored on the
    origins that existed then, so the number is comparable with the one in the module doc, and it
    is the number the review asked for: a semiconductor fix that moves an ordinary reading is a
    regression, not a trade."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from purpose_phrases import ADDED_AFTER_221, PHRASES

    was = [(p, lab) for p, lab, where in PHRASES if where not in ADDED_AFTER_221]
    exact = sum(_label(p) == lab for p, lab in was)
    assert len(was) == 230, "the set the 221/230 was measured on has changed size"
    assert exact >= 221, "\n".join(f"   {p!r} wanted {lab}, got {_label(p)}"
                                   for p, lab in was if _label(p) != lab)
