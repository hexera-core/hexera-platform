# Responsibility: Verify the user's own notes are asked once, kept word for word, shown to a step as guidance and never turned into a check.
from __future__ import annotations

import pytest

from meshpipeline.contracts import user_notes as un

#: A pasted spec in the spirit of a real customer's (synthetic): several lines, units, solver-only
#: items mixed with mesh-relevant ones. It must survive exactly as typed.
SPEC = ("Bracket assembly, 6 solids, mm.\n"
        "- The small vent holes on the side panel can be removed for meshing.\n"
        "- Base plate is the fixed component; the ribs are welded to it.\n"
        "- Load: 900 daN on the top lug, ramped over 3 s, held 0.5 s.\n\n"
        "Outcome 1: peak von Mises in the welds.")


def _asked(answer: str) -> list[dict]:
    return [{"role": "user", "content": "mesh this bracket for FEA"},
            {"role": "assistant", "content": un.QUESTION},
            {"role": "user", "content": answer}]


def test_a_pasted_spec_is_kept_exactly_as_typed():
    notes, unmatched = un.capture(_asked(SPEC))
    assert unmatched == []
    assert [n["text"] for n in notes] == [SPEC]
    assert notes[0]["source"] == un.SOURCE_CHAT and notes[0]["turn"] == 2 and notes[0]["at"]


@pytest.mark.parametrize("answer", ["no", "No.", "nope", "nothing specific", "no thanks",
                                    "No, that's all", "skip", "go ahead", "ok"])
def test_a_no_is_not_a_note(answer):
    assert un.is_skip(answer)
    assert un.capture(_asked(answer)) == ([], [])


@pytest.mark.parametrize("answer", ["no layers on the inlet please", "Use 12 prism layers",
                                    "nothing coarser than 2 mm near the weld"])
def test_an_answer_that_starts_with_no_but_says_something_is_kept(answer):
    assert not un.is_skip(answer)
    assert [n["text"] for n in un.capture(_asked(answer))[0]] == [answer]


def test_the_question_is_found_once_and_only_its_answer_is_taken():
    msgs = _asked("no") + [{"role": "assistant", "content": "Summary..."},
                           {"role": "user", "content": "change the fluid to water"}]
    assert un.asked(msgs) and un.answer(msgs) == (2, "no")
    assert not un.answers_now(msgs)
    assert un.answers_now(_asked("no"))
    assert not un.asked([{"role": "user", "content": "hi"}])


def test_a_quote_keeps_the_whole_message_and_a_made_up_quote_is_refused():
    msgs = [{"role": "user", "content": "Pipe, internal flow.\nUse 12 prism layers on the wall."},
            {"role": "user", "content": "yes"}]
    notes, unmatched = un.capture(msgs, ["12  PRISM layers", "first cell 0.1 mm"])
    assert [n["text"] for n in notes] == [msgs[0]["content"]] and notes[0]["turn"] == 1
    assert unmatched == ["first cell 0.1 mm"]


def test_synthetic_nudges_are_never_the_users_words():
    msgs = [{"role": "assistant", "content": un.QUESTION},
            {"role": "user", "content": "[SYSTEM] something", "_synthetic": True}]
    assert un.answer(msgs) is None and un.capture(msgs) == ([], [])


def test_merge_keeps_every_note_once_and_never_rewords():
    chat = un.capture(_asked(SPEC))[0]
    box = [un.make("Solver: Abaqus/Explicit", source=un.SOURCE_CONSOLE)]
    merged = un.merge(chat, box)
    assert [(n["id"], n["text"]) for n in merged] == [("n1", SPEC), ("n2", "Solver: Abaqus/Explicit")]
    # the same chat note captured again at the next submission is not a second note
    again = un.merge(merged, un.capture(_asked(SPEC))[0])
    assert [n["text"] for n in again] == [SPEC, "Solver: Abaqus/Explicit"]
    assert again[0]["at"] == merged[0]["at"]
    assert un.clean([{"text": "  "}, "junk", None]) == []


def test_a_step_is_shown_them_verbatim_as_guidance_never_as_criteria(tmp_path):
    notes = un.merge([], un.capture(_asked(SPEC))[0])
    block = un.prompt_block(notes, consumer="fea_ai")
    assert SPEC in block and "GUIDANCE" in block and "not acceptance criteria" in block
    assert un.prompt_block([], consumer="fea_ai") == ""
    ctx = un.context_block(notes, consumer="reviewer")
    assert SPEC in ctx and "CONTEXT ONLY" in ctx and "Never fail" in ctx
    with pytest.raises(KeyError):
        un.prompt_block(notes, consumer="somebody_new")


def test_every_workspace_gets_the_file_and_a_run_without_notes_gets_none(tmp_path):
    notes = un.clean([un.make(SPEC, source=un.SOURCE_CONSOLE)])
    assert un.write_file(tmp_path, notes)
    text = (tmp_path / un.NOTES_FILE).read_text(encoding="utf-8")
    assert SPEC in text and "typed in the notes box" in text
    assert SPEC in un.workspace_block(tmp_path, consumer="snappy_planner")
    assert not un.write_file(tmp_path, [])
    assert not (tmp_path / un.NOTES_FILE).exists()
    assert un.workspace_block(tmp_path, consumer="snappy_planner") == ""


def test_a_remark_is_one_plain_line_best_effort(tmp_path):
    un.record_remark(tmp_path, "used 12 prism layers;\n the vent holes stay", by="builder")
    un.record_remark(tmp_path, "", by="fea_ai")                       # nothing said, nothing kept
    un.record_remark(tmp_path, "a second word from the builder", by="builder")
    assert un.read_remarks(tmp_path) == ["a second word from the builder"]
    assert un.result_lines(un.read_remarks(tmp_path)) == [
        "From your notes: a second word from the builder"]
    (tmp_path / un.REMARKS_FILE).write_text("not json")
    assert un.read_remarks(tmp_path) == [] and un.read_remarks("") == []
