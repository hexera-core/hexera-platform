# Responsibility: Verify the mesh viewer's camera controls in a real browser - which mouse button does
# what, the "Up" axis the user picks, and the "Colour by" line that switches the heatmap on.
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

    # the "Colour by" line on the canvas switches the heatmap on, shows which one is on, and the
    # scale's close button turns it off again; it sits clear of the toolbar above and the hint below
    chip = live.evaluate(f"""(() => {{
      const ctl = document.getElementById('v-heatctl-{JOB}');
      const c = ctl.querySelector('a[data-metric=non_ortho]');
      const box = s => document.querySelector(s).getBoundingClientRect();
      const state = () => [c.classList.contains('on'), c.getAttribute('aria-pressed'),
                           !!document.querySelector('#viewer-{JOB} .v-legend')];
      const r = {{text: ctl.textContent, role: c.getAttribute('role'), off: state(),
                  ctlTop: box('#v-heatctl-{JOB}').top, ctlBottom: box('#v-heatctl-{JOB}').bottom,
                  barBottom: box('#viewer-{JOB} .vt').bottom, hintTop: box('#v-hint-{JOB}').top}};
      c.click(); r.on = state();
      document.querySelector('#viewer-{JOB} .v-legend .x').click(); r.closed = state();
      c.click(); r.again = state(); c.click(); r.offAgain = state();
      return r;
    }})()""")
    assert chip["text"] == "Colour by non-orthogonality · skewness · aspect ratio", chip
    assert chip["role"] == "button" and chip["off"] == [False, "false", False], chip
    assert chip["on"] == [True, "true", True] and chip["closed"] == [False, "false", False], chip
    assert chip["again"] == [True, "true", True] and chip["offAgain"] == [False, "false", False], chip
    assert chip["ctlTop"] >= chip["barBottom"] + 4 and chip["ctlBottom"] <= chip["hintTop"] - 4, chip
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


def test_the_way_up_is_remembered_for_the_conversation_not_the_run(live, tmp_path):
    # THE PART AND ITS MESH AGREE. The geometry check stores the user's "Up" against the session;
    # the mesh viewer used to store it against the job, so the same file came back the other way
    # up once it was meshed. Both read the session now, and a choice saved under the old job key
    # is migrated into it once rather than lost.
    live.evaluate("""(async () => {
      const s = await import('/static/js/core/state.js');
      localStorage.removeItem('hexera.view-up.session.conv-1');
      localStorage.removeItem('hexera.view-up.session.conv-2');
      localStorage.removeItem('hexera.view-up.job.sess-job');
      // the geometry check's own saved choice for this conversation
      localStorage.setItem('hexera.view-up.session.conv-1', '+y');
      // a choice saved by the old build, keyed by the run
      localStorage.setItem('hexera.view-up.job.old-job', '-z');
      s.beginRun('sess-job', {sessionId: 'conv-1'});
      s.beginRun('old-job', {sessionId: 'conv-2'});
    })()""")
    _open(live, _payload(tmp_path), "sess-job")
    _open(live, _payload(tmp_path), "old-job")
    got = live.evaluate("""(() => ({
      fromSession: document.getElementById('v-up-sess-job').value,
      migrated: document.getElementById('v-up-old-job').value,
      written: localStorage.getItem('hexera.view-up.session.conv-2'),
      oldKept: localStorage.getItem('hexera.view-up.job.old-job')}))()""")
    # the check's session choice opens the delivered mesh, with nothing stored for the job
    assert got["fromSession"] == "+y", got
    # the old job-scoped choice is adopted, and written under the session so the part sees it too
    assert got["migrated"] == "-z" and got["written"] == "-z", got
    assert got["oldKept"] == "-z", got

    # and what the user picks on the mesh is stored for the conversation, not for the run
    now = live.evaluate("""(() => {
      window._vdbg['sess-job'].setUp('-y');
      return {session: localStorage.getItem('hexera.view-up.session.conv-1'),
              job: localStorage.getItem('hexera.view-up.job.sess-job')}; })()""")
    assert now["session"] == "-y" and now["job"] is None, now
    live.evaluate("['hexera.view-up.session.conv-1','hexera.view-up.session.conv-2',"
                  "'hexera.view-up.job.old-job'].forEach(k => localStorage.removeItem(k))")
    assert_clean(live, "the way up remembered for the conversation")
