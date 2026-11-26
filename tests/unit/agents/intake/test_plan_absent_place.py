# Responsibility: Verify NOTE-23: words that name a kind of place this part does not have ("finer at the fillets" on a
#       flat perforated panel with holes and notches and no fillet) are never spread over the whole part: nothing is
#       set, the checklist line stays "Not set yet", the reply says none was found, what was found and asks for a click,
#       and a click sent alone next applies the waiting words to the clicked face (NOTE-22). A kind the part has, words
#       with no place, and a model whose faces were not measured (the flow route) are read as before.
from __future__ import annotations

import json

import pytest

from meshpipeline.agents.intake import plan_reply as PR
from meshpipeline.agents.intake import plan_talk as PT
from meshpipeline.application import part_matches as PM
from meshpipeline.cad import part_words as PW
from meshpipeline.contracts import user_notes as UN
from meshpipeline.engines import mesh_requirements as MR

SAID = "finer at the fillets"
#: the panel as measured: one body, 8 alike holes + 2 holes (10 holes) and 4 notches, no fillet
PANEL = [{"id": "Part_1", "name": "", "faces": 31, "size_mm": [200.0, 120.0, 2.0], "typical_wall_mm": 2.0},
         {"id": "Feature_1", "feature": True, "kind": "hole group", "count": 8, "in_part": "Part_1",
          "name": "8 holes, 6 mm across"},
         {"id": "Feature_2", "feature": True, "kind": "hole", "count": 1, "in_part": "Part_1", "name": "a hole, 12 mm"},
         {"id": "Feature_3", "feature": True, "kind": "hole", "count": 1, "in_part": "Part_1", "name": "a hole, 20 mm"},
         *[{"id": f"Feature_{k}", "feature": True, "kind": "notch", "count": 1, "in_part": "Part_1",
            "name": "a notch, radius 3 mm"} for k in range(4, 8)]]
FACE = {"id": "Face_7", "label": "Face 7 · round R3 mm", "sig": {"area": 1.0, "c": [0, 0, 0]}}
ASKED = {"text": SAID, "source": "chat", "turn": 1}


def _check(quote, table=PANEL, parts=("Part_1",), confidence=0.8, ask="refine"):
    # the reader, finding no fillet, tied the words to the whole panel (as on the deployed build, 91926483)
    args = json.dumps({"phrases": [{"parts": list(parts), "everything_else": False, "ask": ask, "quote": quote,
                                    "why": "the part", "confidence": confidence, "extent": "whole",
                                    "extent_words": quote, **({"min": 2, "max": 2} if ask == "size" else {})}],
                       "words": []})
    unsure: list[dict] = []
    entries, dropped, _w = PW.check(args, list(table), quote, unsure)
    return entries, dropped, unsure


def _card(notes, quote=SAID):
    _e, _d, unsure = _check(quote)
    own = UN.clean(notes)
    reading = {"status": "used", "entries": _e, "unsure": unsure}
    return {**PM.with_notes(PM.with_faces(PM.rows_of(reading, {"table": PANEL}, "s1"), own), own), "status": "done"}


def test_the_rule_id_is_cited():
    assert PW.NONE_HERE_RULE == "NOTE-23"


@pytest.mark.parametrize("quote,noun", [
    (SAID, "fillet"), ("make the fillet finer", "fillet"), ("2 mm on the bosses", "boss"),
    ("finer at the shaft", "shaft"), ("finer on the flange", "flange"),
    ("finer at the bends", "bend"), ("finer at the bend", "bend")])
def test_a_kind_the_part_does_not_have_sets_nothing(quote, noun):
    entries, dropped, unsure = _check(quote, ask="size" if "mm" in quote else "refine")
    assert entries == [] and dropped
    assert len(unsure) == 1 and unsure[0]["none_of"] == noun and unsure[0]["found"] == "10 holes and 4 notches"


@pytest.mark.parametrize("quote", ["finer at the holes", "finer at the notches", "finer at the bores"])
def test_a_kind_the_part_has_is_read_as_before(quote):
    entries, _d, unsure = _check(quote, parts=["Feature_1", "Feature_2", "Feature_3"])
    assert len(entries) == 1 and unsure == []


def test_no_place_word_is_the_whole_part_as_before():
    entries, _d, unsure = _check("make it finer")
    assert [e["parts"] for e in entries] == [["Part_1"]] and unsure == []


def test_a_model_whose_faces_were_not_measured_is_read_as_before():
    # the flow route's staged STL has no CAD faces: no area is found there, so none missing can be told
    flow = [{"id": "Part_1", "name": "", "faces": 0}]
    entries, _d, unsure = _check(SAID, table=flow)
    assert [e["parts"] for e in entries] == [["Part_1"]] and unsure == []


def test_the_flow_routes_role_words_are_read_as_before():
    flow = [{"id": "Part_1", "name": "", "faces": 0}, {"id": "Part_2", "name": "outlet", "faces": 0}]
    entries, _d, unsure = _check("finer at the outlet", table=flow, parts=["Part_2"])
    assert [e["parts"] for e in entries] == [["Part_2"]] and unsure == []


def test_a_plate_is_its_own_thin_wall_and_a_block_has_none():
    # a part that is one plate offers no thin-wall area: its walls are the part, never "none found"
    entries, _d, unsure = _check("refine the thin walls")
    assert [e["parts"] for e in entries] == [["Part_1"]] and unsure == []
    block = [{**PANEL[0], "size_mm": [200.0, 120.0, 60.0], "typical_wall_mm": 20.0}, *PANEL[1:]]
    entries, _d, unsure = _check("refine the thin walls", table=block)
    assert entries == [] and unsure[0]["none_of"] == "thin wall"


def test_the_checklist_stays_not_set_and_the_reply_says_none_and_asks_for_a_click():
    card = _card([ASKED])
    assert card["rows"] == [] and len(card["unsure"]) == 1
    assert card["unsure"][0]["text"].startswith("Not set yet")
    plain = PT.reply_for(ASKED, card, note_id="n1")
    assert plain == ("I couldn't find any fillets on this part (it has 10 holes and 4 notches), so I haven't set that "
                     "anywhere. Click the face you mean and send that again.")
    assert "whole part" not in plain and "I've set" not in plain
    assert PT.none_found(ASKED, card, note_id="n1")


def test_the_reply_is_the_fixed_line_never_the_models():
    import asyncio
    card = _card([ASKED])
    got = asyncio.run(PR.written(ASKED, card, note_id="n1"))
    assert got["source"] == PR.SOURCE_FIXED and got["text"] == PT.reply_for(ASKED, card, note_id="n1")


def test_the_reply_leaves_the_ask_waiting_and_a_click_alone_applies_it_to_that_face():
    card = _card([ASKED])
    waiting = PT.waiting_ask(ASKED, card, note_id="n1")
    assert waiting == {"words": SAID, "noun": "fillet", "n": 0, "turns_left": 1}
    assert PT.after_turn(waiting, text="", faces=True) == (SAID, None)
    click = UN.make("", source=UN.SOURCE_CHAT, turn=2, faces=[FACE], answers=SAID)
    notes = [ASKED, click]
    after = _card(notes)
    assert after["unsure"] == []
    assert len(after["rows"]) == 1 and after["rows"][0]["faces"][0]["id"] == "Face_7"
    assert PT.reply_for(click, after, note_id="n2") == "I've set a finer mesh on Face 7 (round R3 mm)."
    assert [x["id"] for x in MR.picked_for(SAID, UN.clean(notes))] == ["Face_7"]
