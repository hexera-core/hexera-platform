"""The 900-character receipt was printed again above the next question, verbatim.

MEASURED on ahmed_variant_001: the customer answered something that moved the purpose, intake called
`survey_the_part` a second time, and turns 2 and 3 both carried an identical THE SURVEYOR block -
1,875 and 1,090 characters, of which 900 was the same panel twice. Turn 3's own content was one
sentence. The code meant to show it once (`if not st.surveyor_panel`) but `_exec_state` is rebuilt
every turn, so that guard only ever stopped a second copy inside a single turn.

This pins the behaviour at the composition point rather than through a whole turn, because what went
wrong is a question about the conversation so far and nothing else: has this customer already been
shown this panel.
"""
from __future__ import annotations

from meshpipeline.agents.intake.agent import _MARK
from meshpipeline.agents.intake.agent import reply_with_the_receipt as _compose

PANEL = f"""
{_MARK}

MEASURED (arithmetic on your file, not an opinion)
  1,044 x 389 x 288 mm
  4 opening(s): o1, o2, o3, o4
"""


def test_the_first_showing_carries_the_whole_panel():
    out = _compose(PANEL, "Which side is the fluid on?", [])
    assert _MARK in out
    assert "1,044 x 389 x 288 mm" in out
    assert "changed" not in out, "nothing had changed - there was nothing to show it against"


def test_the_second_turn_does_not_repeat_an_unchanged_panel():
    first = _compose(PANEL, "Which side is the fluid on?", [])
    second = _compose(PANEL, "Do you want to select snappyHexMesh?", [first])
    assert _MARK not in second, "the receipt was printed twice, which is the whole defect"
    assert second == "Do you want to select snappyHexMesh?", (
        "the turn should be its own one sentence and nothing else")


def test_a_panel_that_actually_changed_is_shown_again_and_labelled():
    """The forecast moving is news. An unlabelled second copy is what the defect looked like."""
    first = _compose(PANEL, "Which side is the fluid on?", [])
    moved = PANEL.replace("4 opening(s): o1, o2, o3, o4", "2 opening(s): o1, o2")
    second = _compose(moved, "Still snappyHexMesh?", [first])
    assert _MARK in second, "a changed measurement is worth showing"
    assert "the measurement or the reading changed" in second, (
        "shown again without saying why is indistinguishable from the bug")
    assert "2 opening(s): o1, o2" in second


def test_it_is_the_panel_that_is_matched_and_not_merely_its_header():
    """Matching the header alone would suppress a genuinely changed panel for ever."""
    moved = PANEL.replace("1,044", "2,088")
    assert _MARK in moved
    second = _compose(moved, "ok?", [_compose(PANEL, "first", [])])
    assert "2,088" in second, "a changed panel was suppressed because its header looked familiar"


def test_an_unrelated_assistant_turn_does_not_count_as_having_shown_it():
    out = _compose(PANEL, "Which side is the fluid on?", ["CFD it is. Which mesher do you use?"])
    assert _MARK in out


def test_an_empty_panel_is_never_treated_as_already_shown():
    """`"" in anything` is True, so a falsy panel must not decide this by containment."""
    out = _compose("", "Which side is the fluid on?", ["anything at all"])
    assert out.strip() == "Which side is the fluid on?"
