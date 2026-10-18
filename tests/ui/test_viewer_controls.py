# Responsibility: Verify the mesh viewer's camera controls in a real browser - which mouse button does
# what, the "Up" axis the user picks, and the figures that switch the heatmap on.
# Boundaries: the payload is the backend's own for a known mesh; only the network is stood in for.
from __future__ import annotations

import math

import pytest
from conftest import assert_clean
from test_heatmap import _open, _payload

pytestmark = pytest.mark.ui

JOB = "ctl-job"
V = f"window._vdbg['{JOB}']"


def _angle(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b)) / (math.hypot(*a) * math.hypot(*b))
    return math.degrees(math.acos(max(-1.0, min(1.0, dot))))


def test_right_drag_pans_left_drag_turns_and_the_up_axis_is_the_users(live, tmp_path):
    _open(live, _payload(tmp_path), JOB)

    # the hint says the mapping in plain words
    hint = live.evaluate(f"document.getElementById('v-hint-{JOB}').textContent")
    assert hint == "Rotate: drag · Pan: right-drag · Zoom: scroll", hint

    cx, cy = live.evaluate(f"""(() => {{
      const r = document.getElementById('v-canvas-{JOB}').getBoundingClientRect();
      return [r.left + r.width / 2, r.top + r.height / 2]; }})()""")

    def drag(button: str, dx: float, dy: float) -> dict:
        # real input through the DevTools protocol: the browser makes the pointer events, so vtk's
        # interactor sees exactly what a mouse would give it
        bits = {"left": 1, "right": 2}[button]
        send = live._send
        send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": cx, "y": cy})
        send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": cx, "y": cy, "button": button,
                                          "buttons": bits, "clickCount": 1})
        for i in range(1, 9):
            send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": cx + dx * i / 8, "y": cy + dy * i / 8,
                                              "button": button, "buttons": bits})
        send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": cx + dx, "y": cy + dy, "button": button,
                                          "buttons": 0, "clickCount": 1})
        return live.evaluate(f"{V}.view()")

    start = live.evaluate(f"{V}.view()")
    # RIGHT-DRAG PANS: the point the camera looks at moves, the direction it looks in does not
    panned = drag("right", 40, 0)
    assert _angle(start["dir"], panned["dir"]) < 0.01, (start, panned)
    assert math.dist(start["focal"], panned["focal"]) > 1e-4, (start, panned)
    # LEFT-DRAG TURNS the part, as it always did
    turned = drag("left", 60, 0)
    assert _angle(panned["dir"], turned["dir"]) > 1.0, (panned, turned)

    # WHICH WAY IS UP: the canvas's selector turns the part, Fit keeps the choice, and it is
    # remembered for this job in this browser
    up = live.evaluate(f"""(() => {{
      const v = {V};
      const sel = document.getElementById('v-up-{JOB}');
      sel.value = '-z'; sel.dispatchEvent(new Event('change'));
      const flipped = v.up();
      document.getElementById('v-fitbtn-{JOB}').click();
      const r = {{flipped, fit: v.up(), axis: v.upAxis(),
                  kept: localStorage.getItem('hexera.view-up.job.{JOB}')}};
      v.setUp('+y'); r.yUp = v.up();
      v.setUp('+z'); r.back = v.up();
      return r;
    }})()""")
    assert up["flipped"] == pytest.approx([0, 0, -1], abs=1e-6), up
    assert up["fit"] == pytest.approx([0, 0, -1], abs=1e-6) and up["axis"] == "-z" and up["kept"] == "-z", up
    assert up["yUp"] == pytest.approx([0, 1, 0], abs=1e-6), up
    assert up["back"] == pytest.approx([0, 0, 1], abs=1e-6), up

    # the figure that colours the mesh says so, and says when it is on
    chip = live.evaluate(f"""(() => {{
      const c = document.querySelector('#v-facts-{JOB} .mx-c.live[data-metric=non_ortho]');
      const t = () => c.querySelector('.mx-show-t').textContent;
      const r = {{off: t(), role: c.getAttribute('role'), label: c.querySelector('.mx-k').textContent}};
      c.click(); r.on = t(); r.pressed = c.getAttribute('aria-pressed');
      c.click(); r.again = t();
      return r;
    }})()""")
    assert chip["off"] == "Show on mesh" and chip["role"] == "button", chip
    assert chip["label"] == "Max non-orthogonality", chip
    assert chip["on"] == "Showing" and chip["pressed"] == "true" and chip["again"] == "Show on mesh", chip
    assert_clean(live, "the viewer's camera controls")


def test_the_way_up_the_check_proposed_opens_the_viewer_until_the_user_chooses_another(live, tmp_path):
    # the geometry check proposes which axis is up (surf.up_axis); with nothing stored for the job
    # the viewer opens that way up, and a choice the user stored for the job still wins
    live.evaluate("localStorage.removeItem('hexera.view-up.job.up-job'); "
                  "localStorage.setItem('hexera.view-up.job.kept-job', '+y')")
    proposed = _payload(tmp_path)
    proposed["up_axis"] = "-z"
    _open(live, proposed, "up-job")
    _open(live, dict(proposed), "kept-job")
    got = live.evaluate("""(() => ({
      proposed: {sel: document.getElementById('v-up-up-job').value, up: window._vdbg['up-job'].up()},
      kept: {sel: document.getElementById('v-up-kept-job').value, up: window._vdbg['kept-job'].up()}}))()""")
    assert got["proposed"]["sel"] == "-z", got
    assert got["proposed"]["up"] == pytest.approx([0, 0, -1], abs=1e-6), got
    assert got["kept"]["sel"] == "+y", got
    assert got["kept"]["up"] == pytest.approx([0, 1, 0], abs=1e-6), got
    live.evaluate("localStorage.removeItem('hexera.view-up.job.kept-job')")
    assert_clean(live, "the viewer opening the proposed way up")
