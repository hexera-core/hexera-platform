# Responsibility: Verify in a real browser that the mesh viewer's on-canvas controls leave the camera
# exactly where the user put it - dragging the Section slider moves only the cut, and a drag or a
# keystroke inside any overlay or the picked-part chip never turns, pans, zooms or resets the view.
# Boundaries: the payload is the backend's own for three unit hexes in a row; only the network is
# stood in for. It reads the camera and the cut through the viewer's debug hooks, never pixels.
from __future__ import annotations

import pytest
from conftest import assert_clean
from test_heatmap import _open, _payload

pytestmark = pytest.mark.ui

JOB = "cam-job"
T = f"window._vdbg['{JOB}:tools']"


def _ready(live, tmp_path):
    _open(live, _payload(tmp_path, shear=0.0), JOB)
    live.wait_for(T, timeout=30, what="the viewer toolbar")


def _centre(live, selector_js: str) -> tuple[float, float]:
    return live.evaluate(f"""(() => {{ const r = {selector_js}.getBoundingClientRect();
      return [r.left + r.width / 2, r.top + r.height / 2]; }})()""")


def _drag(live, x0: float, y0: float, x1: float, y1: float, steps: int = 8) -> None:
    # a real left-button drag by the browser's own mouse, so every listener sees what a user's does
    send = live._send
    send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x0, "y": y0})
    send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x0, "y": y0, "button": "left",
                                      "buttons": 1, "clickCount": 1})
    for i in range(1, steps + 1):
        x, y = x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps
        send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y, "button": "left", "buttons": 1})
    send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x1, "y": y1, "button": "left",
                                      "buttons": 0, "clickCount": 1})
    live.settle(0.2)


def _bare_spot(live, selector_js: str) -> tuple[float, float]:
    # a point on the element's own panel - not on a control in it, and not in a rounded corner the
    # browser does not count as the element - checked with the browser's own hit test
    spot = live.evaluate(f"""(() => {{ const el = {selector_js}, r = el.getBoundingClientRect();
      for (let y = r.top + 14; y < r.bottom - 2; y += 2) for (const x of [r.left + 4, r.left + 6, r.right - 4]) {{
        const hit = document.elementFromPoint(x, y);
        if (hit && el.contains(hit) && !hit.closest('button,input,select,label,a')) return [x, y]; }}
      return null; }})()""")
    assert spot, f"no bare spot on {selector_js}"
    return spot


def _open_tool(live, tool: str) -> None:
    x, y = _centre(live, f"document.getElementById('v-{JOB}-vt-{tool}')")
    live.click(x, y)
    live.wait_for(f"{T}.current() === '{tool}'", timeout=5, what=f"the {tool} overlay")


def _same_camera(before: dict, after: dict, what: str) -> None:
    for k in ("position", "focal", "up"):
        assert after[k] == pytest.approx(before[k], abs=1e-9), f"{what} moved the camera's {k}: {before} -> {after}"
    for k in ("angle", "scale", "parallel"):
        assert after[k] == pytest.approx(before[k], abs=1e-9), f"{what} changed the camera's {k}: {before} -> {after}"


def _turn_and_zoom(live) -> None:
    # the user has set a view of their own first: a turn on the part and a scroll in, so a control
    # that re-frames, resets or re-aims the camera cannot land back on the same numbers by chance
    cx, cy = _centre(live, f"document.getElementById('v-canvas-{JOB}')")
    _drag(live, cx - 60, cy + 40, cx + 30, cy + 10)
    for _ in range(2):
        live._send("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": cx, "y": cy, "deltaX": 0, "deltaY": -120})
    live.settle(0.3)


def test_dragging_the_section_slider_moves_the_cut_and_never_the_camera(live, tmp_path):
    _ready(live, tmp_path)
    _turn_and_zoom(live)
    _open_tool(live, "section")
    # Cut across X, as the founder did
    live.click(*_centre(live, f"document.querySelector('#viewer-{JOB} .v-ov-section .vo-seg[data-seg=cut-axis] button[data-v=x]')"))
    before, cut0 = live.evaluate(f"{T}.camera()"), live.evaluate(f"{T}.cut()")
    assert cut0["on"] and cut0["axis"] == "x" and cut0["at"] == pytest.approx(0.5), cut0

    # drag the slider's thumb from the middle most of the way to the left, then back right a little
    slider = f"document.getElementById('v-cut-at-{JOB}')"
    x, y = _centre(live, slider)
    w = live.evaluate(f"{slider}.getBoundingClientRect().width")
    _drag(live, x, y, x - 0.35 * w, y + 12)
    mid = live.evaluate(f"{T}.camera()"), live.evaluate(f"{T}.cut()")
    _drag(live, x - 0.35 * w, y, x - 0.2 * w, y - 12)
    after, cut1 = live.evaluate(f"{T}.camera()"), live.evaluate(f"{T}.cut()")
    # a hand that overshoots: the thumb pressed and dragged right off the overlay onto the part
    h = live.evaluate(f"document.querySelector('#viewer-{JOB} .v-ov-section').getBoundingClientRect().bottom")
    _drag(live, x - 0.2 * w, y, x - 0.2 * w, h + 120, steps=12)
    _same_camera(before, live.evaluate(f"{T}.camera()"), "a slider drag that ran off onto the part")

    # the cut moved through the part...
    assert mid[1]["on"] and mid[1]["at"] < 0.25, mid[1]
    assert cut1["on"] and 0.2 < cut1["at"] < 0.45 and cut1["at"] != mid[1]["at"], cut1
    assert set(cut1["planes"]) == {1} and cut1["lines"] >= 1, cut1
    # ...and the camera stayed exactly where the user left it, during the drag and after it
    _same_camera(before, mid[0], "the first slider drag")
    _same_camera(before, after, "the second slider drag")
    # the overlay is still open: a drag on its own control is not a click outside
    assert live.evaluate(f"{T}.current()") == "section"

    # and the next drag on the part itself still turns it: the slider left no button held down
    live.evaluate(f"{T}.close()")
    cx, cy = _centre(live, f"document.getElementById('v-canvas-{JOB}')")
    _drag(live, cx - 40, cy + 30, cx + 40, cy + 30)
    turned = live.evaluate(f"{T}.camera()")
    assert turned["position"] != pytest.approx(after["position"], abs=1e-6), "a drag on the part no longer turns it"
    assert_clean(live, "the Section slider")


@pytest.mark.parametrize("tool", ["view", "section", "parts", "measure", "snapshot"])
def test_a_drag_across_an_open_overlay_leaves_the_camera_alone(live, tmp_path, tool):
    _ready(live, tmp_path)
    _turn_and_zoom(live)
    _open_tool(live, tool)
    before = live.evaluate(f"{T}.camera()")
    # press on the overlay's own panel (its padding, not a control) and sweep across it and out
    # onto the part below it
    pane = f"document.querySelector('#viewer-{JOB} .v-ov-{tool}')"
    x, y = _bare_spot(live, pane)
    bottom = live.evaluate(f"{pane}.getBoundingClientRect().bottom")
    _drag(live, x, y, x + 140, bottom + 80, steps=16)
    _same_camera(before, live.evaluate(f"{T}.camera()"), f"a drag on the {tool} overlay")
    if tool == "measure":
        assert live.evaluate(f"{T}.measured().points") == [], "a drag on the Measure overlay measured"
    assert_clean(live, f"the {tool} overlay")


def test_typing_in_the_parts_search_never_resets_the_view_or_restyles_the_part(live, tmp_path):
    _ready(live, tmp_path)
    _turn_and_zoom(live)
    _open_tool(live, "parts")
    before, style = live.evaluate(f"{T}.camera()"), live.evaluate(f"{T}.surface()")
    assert live.evaluate("document.activeElement.id") == f"v-parts-q-{JOB}"
    # vtk's own keys: r resets the camera, w turns every actor to wire, s back to surface
    for ch in "rws":
        live.press(ch, code=f"Key{ch.upper()}", vk=ord(ch.upper()), text=ch)
    assert live.evaluate(f"document.getElementById('v-parts-q-{JOB}').value") == "rws"
    _same_camera(before, live.evaluate(f"{T}.camera()"), "typing in the parts search")
    assert live.evaluate(f"{T}.surface()") == style
    assert_clean(live, "the Parts search")


def test_the_picked_part_chip_leaves_the_camera_alone(live, tmp_path):
    _ready(live, tmp_path)
    _turn_and_zoom(live)
    _open_tool(live, "parts")
    live.click(*_centre(live, f"document.querySelector('#v-bnd-chips-{JOB} .vo-row[data-label=outlet] .vo-pick')"))
    live.wait_for(f"{T}.selected() === 'outlet'", timeout=5, what="the outlet picked")
    live.evaluate(f"{T}.close()")
    before = live.evaluate(f"{T}.camera()")
    chip = f"document.getElementById('v-picked-{JOB}')"
    x, y = _bare_spot(live, chip)
    _drag(live, x, y, x + 120, y - 80)
    _same_camera(before, live.evaluate(f"{T}.camera()"), "a drag on the picked-part chip")
    assert live.evaluate(f"{T}.selected()") == "outlet"
    assert_clean(live, "the picked-part chip")
