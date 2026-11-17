# Responsibility: Verify the intake's split in a real browser: the conversation on the left (a third by default)
# and the one viewer on the right from the moment it opens, a divider the user drags that moves both and is
# remembered, the conversation's minimum width, and the split gone with the viewer when the run starts. Also the
# run box's time limit in plain words.
# Boundaries: the viewer box is a bare stand-in (no scene): only the layout is under test here.
from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

OPEN = """(async () => {
  const { holdIntake, takeOver } = await import('/static/js/viewer/intake_view.js');
  const box = document.createElement('div');
  box.className = 'viewer iv-plain'; box.id = 'split-box';
  box.innerHTML = '<div class="v-canvas"></div>';
  holdIntake(box, takeOver(box, null));
})()"""

WIDTHS = """(() => {
  const app = document.getElementById('app').getBoundingClientRect();
  const chat = document.getElementById('stage').getBoundingClientRect();
  const view = document.getElementById('workbench').getBoundingClientRect();
  return {app: app.width, chat: chat.width, view: view.width, left: view.left};
})()"""


def _desktop(live):
    live._send("Emulation.setDeviceMetricsOverride",
               {"width": 1500, "height": 900, "deviceScaleFactor": 1, "mobile": False})
    live.evaluate("localStorage.removeItem('hexera.intake.chat-share')")


def _drag(live, x0, x1, y):
    live._send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x0, "y": y})
    live._send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x0, "y": y, "button": "left",
                                            "buttons": 1, "clickCount": 1})
    for k in range(1, 11):
        x = x0 + (x1 - x0) * k / 10
        live._send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y, "button": "left", "buttons": 1})
    live._send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x1, "y": y, "button": "left",
                                            "buttons": 0, "clickCount": 1})


def test_the_conversation_takes_a_third_and_the_viewer_the_rest(live):
    _desktop(live)
    live.evaluate(OPEN)
    w = live.evaluate(WIDTHS)
    assert abs(w["chat"] - w["app"] / 3) <= 2, w
    assert abs(w["chat"] + w["view"] - w["app"]) <= 2, w
    assert live.evaluate("!!document.querySelector('#workbench .iv-split[role=separator]')")
    assert live.evaluate("getComputedStyle(document.querySelector('.iv-split')).cursor") == "col-resize"


def test_dragging_the_divider_moves_both_and_is_remembered(live):
    _desktop(live)
    live.evaluate(OPEN)
    w0 = live.evaluate(WIDTHS)
    _drag(live, w0["left"], w0["left"] + 250, 400)
    w1 = live.evaluate(WIDTHS)
    assert abs(w1["chat"] - (w0["chat"] + 250)) <= 3, (w0, w1)
    assert abs(w1["view"] - (w0["view"] - 250)) <= 3, (w0, w1)
    kept = float(live.evaluate("localStorage.getItem('hexera.intake.chat-share')"))
    assert abs(kept - w1["chat"] / w1["app"]) < 0.01
    # a new intake opens at the width the user left it
    live.evaluate("""(async () => { const { endIntake } = await import('/static/js/viewer/intake_view.js'); endIntake(); })()""")
    live.evaluate(OPEN)
    w2 = live.evaluate(WIDTHS)
    assert abs(w2["chat"] - w1["chat"]) <= 3, (w1, w2)
    # the conversation is never dragged narrower than it can be read
    _drag(live, w2["left"], 20, 400)
    w3 = live.evaluate(WIDTHS)
    assert abs(w3["chat"] - 320) <= 2, w3


def test_the_split_goes_with_the_viewer(live):
    _desktop(live)
    live.evaluate(OPEN)
    out = live.evaluate("""(async () => {
      const { endIntake } = await import('/static/js/viewer/intake_view.js');
      endIntake();
      const app = document.getElementById('app');
      return {box: !!document.getElementById('split-box'), wb: app.classList.contains('wb'),
              intake: app.classList.contains('intake'), divider: !!document.querySelector('.iv-split')};
    })()""")
    assert out == {"box": False, "wb": False, "intake": False, "divider": False}, out


def test_the_run_box_says_its_time_limit_in_plain_words(live):
    # the run box (four rows, #423) has no mesh bar: the line #417 put under the bar is the live note of the
    # row that is creating the mesh
    sub = live.evaluate("""(async () => {
      const { Stage } = await import('/static/js/render/stage.js');
      Stage.mount(); Stage.ensureProc();
      Stage.startMesh('gmsh', 3600, null);
      const n = document.querySelector('.rr-live:not([hidden]) .rr-note');
      return n ? n.textContent : null;
    })()""")
    assert sub == "No similar runs yet · Time limit 60 min", sub
    assert "stops by itself" not in sub
