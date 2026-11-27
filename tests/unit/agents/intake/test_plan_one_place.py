# Responsibility: Verify NOTE-21: words that name ONE place ("make it finer in this hole"), with no face picked, are
#       never spread over the several places the part has: the reading keeps the phrase as not placed (how many, and
#       what), the checklist does not show it set on them, the reply asks for one click (its guard refuses one that
#       does not); words that tell one apart set that one, and plural words still set all.
from __future__ import annotations

import json

import pytest

from meshpipeline.agents.intake import plan_reply as PR
from meshpipeline.agents.intake import plan_talk as PT
from meshpipeline.application import part_matches as PM
from meshpipeline.cad import part_words as PW

#: as typed on the deployed site, with a screenshot and no face picked
SAID = "make it finer in this hole"
HOLES = [{"id": f"Feature_{k}", "feature": True, "kind": "hole", "count": 1, "in_part": f"Part_{1 + k % 3}",
          "name": f"a hole, {6 + k} mm across", "diag_mm": float(6 + k)} for k in range(1, 12)]
TABLE = [{"id": "Part_1", "name": ""}, {"id": "Part_2", "name": ""}, {"id": "Part_3", "name": ""}, *HOLES]
PIC = [{"id": "a1", "kind": "image", "mime": "image/png"}]


def _check(quote, *, parts=(), confidence=0.4, notes=None):
    args = json.dumps({"phrases": [{"parts": list(parts), "everything_else": False, "ask": "refine", "quote": quote,
                                    "why": "a hole", "confidence": confidence}], "words": []})
    unsure: list[dict] = []
    entries, dropped, _w = PW.check(args, TABLE, notes or quote, unsure)
    return entries, dropped, unsure


def test_the_rule_id_is_cited():
    assert PW.ONE_PLACE_RULE == "NOTE-21"


@pytest.mark.parametrize("parts,confidence", [((), 0.4), ([h["id"] for h in HOLES], 0.9)])
def test_one_hole_named_and_eleven_on_the_part_sets_nothing_and_waits_for_a_click(parts, confidence):
    # our own hole tie (the reader unsure) and a reader sure of all eleven: neither spreads it
    entries, dropped, unsure = _check(SAID, parts=parts, confidence=confidence)
    assert entries == [] and dropped
    assert len(unsure) == 1 and unsure[0]["which_one"] == 11 and unsure[0]["noun"] == "hole"


@pytest.mark.parametrize("quote,want", [
    ("make it finer in the 9 mm hole", ["Feature_3"]),        # its own size, in the words
    ("make it finer in the biggest hole", ["Feature_11"]),
    ("make it finer in the smallest hole", ["Feature_1"]),
])
def test_words_that_tell_one_apart_set_that_one(quote, want):
    entries, _d, unsure = _check(quote)
    assert [e["parts"] for e in entries] == [want] and unsure == []


def test_the_hole_in_a_named_part_with_one_hole_there_is_set():
    table = [{"id": "Part_1", "name": ""}, {"id": "Part_2", "name": ""},
             {"id": "Feature_1", "feature": True, "kind": "hole", "count": 1, "in_part": "Part_1", "name": "a hole"},
             {"id": "Feature_2", "feature": True, "kind": "hole", "count": 1, "in_part": "Part_2", "name": "a hole"}]
    q = "finer in the hole in part 2"
    args = json.dumps({"phrases": [{"parts": [], "ask": "refine", "quote": q, "why": "", "confidence": 0.3}]})
    entries, _d, _w = PW.check(args, table, q)
    assert [e["parts"] for e in entries] == [["Feature_2"]]


@pytest.mark.parametrize("quote", ["make the holes finer", "make all holes finer", "every hole finer please",
                                   "finer at each hole"])
def test_plural_words_still_set_every_hole(quote):
    entries, _d, unsure = _check(quote)
    assert len(entries) == 1 and len(entries[0]["parts"]) == 11 and unsure == []


def test_one_hole_named_where_the_part_has_one_hole_is_set_as_before():
    table = [{"id": "Part_1", "name": ""},
             {"id": "Feature_1", "feature": True, "kind": "bore", "count": 1, "in_part": "Part_1", "name": "a bore"}]
    args = json.dumps({"phrases": [{"parts": [], "ask": "refine", "quote": SAID, "why": "", "confidence": 0.3}]})
    entries, _d, _w = PW.check(args, table, SAID)
    assert [e["parts"] for e in entries] == [["Feature_1"]]


def test_a_hole_group_counts_its_holes_and_a_pick_on_the_card_stands():
    table = [{"id": "Part_1", "name": ""}, {"id": "Feature_1", "feature": True, "kind": "hole group", "count": 4,
                                            "in_part": "Part_1", "name": "4 holes, 8 mm across"}]
    args = json.dumps({"phrases": [{"parts": ["Feature_1"], "ask": "refine", "quote": SAID, "why": "", "confidence": 0.9}]})
    unsure: list[dict] = []
    assert PW.check(args, table, SAID, unsure)[0] == [] and unsure[0]["which_one"] == 4
    picked = json.dumps({"phrases": [{"parts": ["Feature_1"], "ask": "refine", "quote": SAID, "why": "picked by you",
                                      "confidence": 1.0}]})
    assert [e["parts"] for e in PW.check(picked, table, SAID)[0]] == [["Feature_1"]]


# the checklist and the reply

def _card():
    _e, _d, unsure = _check(SAID)
    reading = {"status": "used", "entries": [], "unsure": unsure}
    return PM.with_notes(PM.rows_of(reading, {"table": TABLE}, "s1"), [{"text": SAID, "source": "chat", "turn": 2,
                                                                          "attachments": PIC}])


def test_the_checklist_does_not_show_it_set_and_the_reply_asks_for_one_click():
    card = _card()
    assert card["rows"] == [] and len(card["unsure"]) == 1
    line = card["unsure"][0]
    assert line["which_one"] == 11 and "11 holes" in line["text"] and "areas" not in line["text"]
    note = {"text": SAID, "attachments": PIC, "source": "chat", "turn": 2}
    plain = PT.reply_for(note, {**card, "status": "done"}, note_id="n1")
    assert "11 holes" in plain and "Click it on the model" in plain
    assert "I've set" not in plain and "areas" not in plain
    f = PR.facts(note, {**card, "status": "done"}, note_id="n1")
    assert f["applied"] == [] and f["which_one"][0]["how_many_there_are"] == 11
    assert PR.check(plain, f) == ""
    assert PR.check("I've set a finer mesh at 11 areas.", f) != ""                 # what was said on d9d6183b
    assert PR.check("There are 11 holes and I can't tell which one you mean.", f) != ""   # no click asked for
    assert PR.check("There are 11 holes on this part and I can't tell which one you mean from the picture. Click it "
                    "on the model and send that again.", f) == ""
