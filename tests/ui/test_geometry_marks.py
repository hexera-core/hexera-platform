# Responsibility: Verify the console shows the words after the geometry check's marks and never the
# marks, in the conversation a person reads, and leaves ordinary messages alone.
# Boundaries: the shipped modules in a real browser; no network.
from __future__ import annotations

import pytest
from conftest import assert_clean

pytestmark = pytest.mark.ui

DRAWING = "GEOMETRY CHECK (drawing your part): thanks - I'm drawing your part now."
CONFIRMED = "GEOMETRY CHECK (confirmed by the user): the file is the part's wall; the fluid flows through it."


def test_the_marks_are_stripped_in_the_conversation_and_plain_text_is_untouched(live):
    import json
    js = """(async () => {
      const { displayText } = await import('/static/js/render/geometry_form.js');
      const { Stage } = await import('/static/js/render/stage.js');
      Stage.chat('assistant', __DRAWING__);
      Stage.chat('assistant', __CONFIRMED__);
      Stage.chat('assistant', 'Which fluid is it?');
      Stage.chat('user', __DRAWING__);
      const txt = [...document.querySelectorAll('#cc .im.assistant .txt')].map(e => e.textContent.trim());
      const user = [...document.querySelectorAll('#cc .im.user .bub')].map(e => e.textContent.trim());
      return {fn: [displayText(__DRAWING__), displayText('plain'), displayText('')], txt, user};
    })()""".replace("__DRAWING__", json.dumps(DRAWING)).replace("__CONFIRMED__", json.dumps(CONFIRMED))
    out = live.evaluate(js, timeout=60)
    assert out["fn"][0] == "Thanks - I'm drawing your part now." and out["fn"][1] == "plain" and out["fn"][2] == ""
    assert out["txt"][-3] == "Thanks - I'm drawing your part now."
    assert out["txt"][-2].startswith("Confirmed on the picture: the file is the part's wall")
    assert out["txt"][-1] == "Which fluid is it?"
    assert "GEOMETRY CHECK" not in " ".join(out["txt"])
    assert out["user"][-1] == DRAWING            # what a person typed is shown as typed
    assert_clean(live, "the conversation with the marks stripped")


def test_the_hold_follows_what_the_poll_saw(live):
    # a drawing reply that arrives after the poll already knows the outcome installs the hold
    # that matches: none once the check gave up or was confirmed, the stage's once the stage is
    # open, the drawing hold otherwise; and Proceed (releaseHold) opens the box again
    out = live.evaluate("""(async () => {
      const c = await import('/static/js/shell/composer.js');
      c.mountComposer();
      const inp = document.getElementById('chat-input');
      const snap = () => ({disabled: inp.disabled, placeholder: inp.placeholder, held: c.holdState().held});
      const r = {};
      c.noteCheckState(null); c.enableInput(); c.holdForDrawing(); r.drawing = snap();
      c.releaseHold(); r.released = snap();
      c.noteCheckState('over'); c.holdForDrawing(); r.over = snap();
      c.noteCheckState('done'); c.holdForDrawing(); r.done = snap();
      c.noteCheckState('ready'); c.holdForDrawing(); r.ready = snap();
      c.releaseHold(); r.afterProceed = snap();
      return r;
    })()""", timeout=60)
    assert out["drawing"]["disabled"] is True and out["drawing"]["held"] is True
    assert "Drawing your part" in out["drawing"]["placeholder"]
    assert out["released"]["disabled"] is False and out["released"]["held"] is False
    assert out["over"]["disabled"] is False and out["over"]["held"] is False       # the check gave up: no hold
    assert out["done"]["disabled"] is False and out["done"]["held"] is False       # already confirmed: no hold
    assert out["ready"]["disabled"] is True and "press Proceed" in out["ready"]["placeholder"]
    assert out["afterProceed"]["disabled"] is False and out["afterProceed"]["held"] is False
