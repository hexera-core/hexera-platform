# Responsibility: Verify the plan step's conversation (NOTE-18): what one message means (go / ask / requirement /
# the intake's) and the reply said back, built from the checklist rows the system produced for that message.
from __future__ import annotations

import pytest

from meshpipeline.agents.intake import consent as C
from meshpipeline.agents.intake import plan_talk as PT

GO, ASK, NOTE, INTAKE, HOLD, BOTH = PT.GO, PT.ASK, PT.NOTE, PT.INTAKE, PT.HOLD, PT.BOTH

READINGS = [
    # plainly go
    ("proceed", GO),
    ("Proceed", GO),
    ("go", GO),
    ("I'm good to mesh", GO),
    ("all right, I'm good to mesh", GO),
    ("let's mesh", GO),
    ("Let's proceed to mesh generation", GO),
    ("start the mesh", GO),
    ("that's all, run it", GO),
    ("ok great, let's go!", GO),
    ("I'm good", GO),
    ("yes go ahead", GO),
    ("looks good, start meshing", GO),
    ("we're ready to mesh", GO),
    ("nothing else, go ahead", GO),
    ("That's it. Generate the mesh please", GO),
    # a requirement that holds the word go or proceed
    ("go finer on the bore", NOTE),
    ("proceed with 2 mm on the bore", NOTE),
    ("run it at 1 mm", NOTE),
    ("go ahead but make the fillets finer", NOTE),
    ("let's go with 3 layers on the walls", NOTE),
    ("can you do a 1 mm mesh sizing in here?", NOTE),
    ("in that crevice there I want a very refined mesh", NOTE),
    ("keep skewness under 0.8", NOTE),
    ("fine mesh on the inside corner", NOTE),
    # not go: waited (nothing starts, nothing kept), unknown
    ("don't proceed yet", HOLD),
    ("wait", HOLD),
    ("not yet, go later", HOLD),
    ("hold on a second", HOLD),
    ("go to the edge of the plate", NOTE),
    # a plan change with a size: the plan is switched and the size kept
    ("use cfMesh at 2 mm", BOTH),
    # asked back or hedged: asked once
    ("ready to mesh?", ASK),
    ("can we proceed?", ASK),
    ("shall we start the mesh?", ASK),
    ("maybe", ASK),
    # a question about it, or a change to the plan: the intake's
    ("what will the cell count be?", INTAKE),
    ("how long will this take?", INTAKE),
    ("use cfMesh instead", INTAKE),
    ("actually this is an external flow", INTAKE),
]


@pytest.mark.parametrize("text,want", READINGS)
def test_what_a_message_at_the_plan_step_means(text, want):
    assert PT.reading(text, consent=C.reading(text)) == want, text


def test_the_table_is_long_enough_and_covers_every_reading():
    assert len(READINGS) >= 25
    assert {w for _, w in READINGS} == {GO, ASK, NOTE, INTAKE, HOLD, BOTH}


def test_a_message_that_carries_a_picture_or_a_face_is_always_a_requirement():
    assert PT.reading("proceed", carries=True) == NOTE
    assert PT.reading("", carries=True) == NOTE


def test_the_go_question_is_asked_once():
    assert PT.reading("ready to mesh?", asked=False) == ASK
    assert PT.reading("ready to mesh?", asked=True) == NOTE
    assert PT.reading("yes", consent=C.reading("yes"), asked=True) == GO
    assert PT.asked_last([{"role": "assistant", "content": PT.ASK_LINE}, {"role": "user", "content": "x"}])
    assert not PT.asked_last([{"role": "assistant", "content": "Here's the plan."}])


# #
# the reply: what the checklist took from THIS message
# #

FACE32 = {"id": "Face_32", "label": "Face 32 · round Ø6 mm"}


def _done(rows=(), unsure=(), lines=()):
    return {"status": "done", "rows": list(rows), "unsure": list(unsure), "lines": list(lines)}


def test_a_size_on_one_picked_face():
    note = {"text": "can you do a 1 mm mesh sizing in here?", "faces": [FACE32]}
    row = {"phrase": 0, "quote": "can you do a 1 mm mesh sizing in here?", "words": "can you do a 1 mm…",
           "size": "1 mm", "faces": [FACE32], "picked": True, "parts": []}
    assert PT.reply_for(note, _done([row])) == "I've set 1 mm elements on Face 32 (round Ø6 mm)."


def test_a_size_on_several_picked_faces():
    faces = [{"id": f"Face_{k}", "label": f"Face {k} · flat 10 × 10 mm"} for k in (1, 2, 3, 4)]
    note = {"text": "1 mm on these", "faces": faces}
    row = {"quote": "1 mm on these", "size": "1 mm", "faces": faces, "picked": True, "parts": []}
    assert PT.reply_for(note, _done([row])) == "I've set 1 mm elements on the 4 faces you picked."


def test_a_place_the_reader_guessed_says_so_and_how_to_fix_it():
    note = {"text": "in that crevice there I want a very refined mesh"}
    row = {"quote": "in that crevice there I want a very refined mesh", "size": "finer",
           "parts": ["Feature_3", "Feature_5"], "parts_text": "the fillet (radius 8 mm) and the boss (36 mm across)"}
    other = {"quote": "2 mm on the frame rail", "size": "2 mm", "parts": ["Part_9"], "parts_text": "part 9"}
    assert PT.reply_for(note, _done([other, row])) == (
        "I've made the mesh finer at the fillet (radius 8 mm) and the boss (36 mm across). That's my guess from "
        "your words; click it on the model if you meant another spot.")


def test_words_that_are_no_mesh_requirement_are_said_so():
    note = {"text": "make it look nice"}
    other = {"quote": "2 mm on the frame rail", "size": "2 mm", "parts": ["Part_9"], "parts_text": "part 9"}
    got = PT.reply_for(note, _done([other]))
    assert got == PT.NOT_READ and "size" in got and "finer or coarser" in got and "quality limit" in got


def test_a_phrase_it_could_not_place():
    note = {"text": "1 mm on the lip"}
    got = PT.reply_for(note, _done(unsure=[{"quote": "1 mm on the lip", "words": "1 mm on the lip", "size": "1 mm"}]))
    assert got == "I couldn't tell where “1 mm on the lip” is. Click it on the model and send it again."


def test_a_picture_alone_says_what_it_will_and_will_not_do():
    note = {"text": "", "attachments": [{"id": "a1", "kind": "image", "mime": "image/png"}]}
    got = PT.reply_for(note, _done())
    assert got == PT.PICTURE_ONLY and "don't read pictures" in got


def test_words_with_a_picture_say_the_picture_is_kept_but_not_read():
    note = {"text": "finer here", "attachments": [{"id": "a1", "kind": "image", "mime": "image/png"}],
            "faces": [FACE32]}
    row = {"quote": "finer here", "size": "finer", "faces": [FACE32], "picked": True, "parts": []}
    assert PT.reply_for(note, _done([row])) == ("I've made the mesh finer on Face 32 (round Ø6 mm). " + PT.PICTURE_NOTE)
    assert "number" not in PT.PICTURE_NOTE and "don't read pictures" in PT.PICTURE_NOTE


def test_a_flow_mesh_says_the_readers_own_lines_for_these_words():
    note = {"text": "3 layers on the walls"}
    lines = ['From your notes: "3 layers on the walls": 3 layers on the walls.',
             'From your notes: "2 mm on the inlet": cells of 2 mm at the inlet.']
    assert PT.reply_for(note, _done(lines=lines)) == ('"3 layers on the walls": 3 layers on the walls. '
                                                      + PT.CONFIRM_ON_RESULT)


def test_no_checklist_still_reading_and_not_read_yet_never_claim_a_reading():
    note = {"text": "2 mm on the bore", "faces": [FACE32]}
    assert PT.reply_for(note, None) == f"Got it, I've kept 2 mm on Face 32 (round Ø6 mm) for the mesh. {PT.NO_CHECKLIST}"
    assert PT.reply_for(note, {"status": "reading"}) == PT.STILL_READING
    assert PT.reply_for(note, {"status": "ready"}) == PT.READ_BEFORE_RUN


def test_the_rule_id_is_cited():
    assert PT.RULE == "NOTE-18"
