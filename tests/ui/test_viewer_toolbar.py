# Responsibility: Verify the mesh viewer's floating toolbar in a real browser - one overlay at a
# time, Esc and a click outside close it, and each of View, Section, Parts, Measure and Snapshot
# does what it says on a mesh whose shape is known on paper.
# Boundaries: the payload is the backend's own for three unit hexes in a row; only the network is
# stood in for. It asserts what the scene and the page hold, never pixels of the part.
from __future__ import annotations

import math

import pytest
from conftest import assert_clean
from test_heatmap import _open, _payload

pytestmark = pytest.mark.ui

JOB = "tb-job"
T = f"window._vdbg['{JOB}:tools']"
V = f"window._vdbg['{JOB}']"


def _ready(live, tmp_path):
    # shear 0: three unit cubes, x from 0 to 3, y and z from 0 to 1 - every distance is known
    _open(live, _payload(tmp_path, shear=0.0), JOB)
    live.wait_for(f"window._vdbg['{JOB}:tools']", timeout=30, what="the viewer toolbar")


def _btn(tool: str) -> str:
    return f"document.getElementById('v-{JOB}-vt-{tool}')"


def _click_el(live, selector_js: str) -> None:
    # a real click in the middle of an element, through the browser's own mouse
    x, y = live.evaluate(f"""(() => {{ const r = {selector_js}.getBoundingClientRect();
      return [r.left + r.width / 2, r.top + r.height / 2]; }})()""")
    live.click(x, y)


def test_one_overlay_at_a_time_and_esc_or_a_click_outside_closes_it(live, tmp_path):
    _ready(live, tmp_path)
    # the side panel no longer carries a list of parts: the parts are in the toolbar
    seen = live.evaluate(f"""(() => ({{
      tools: [...document.querySelectorAll('#viewer-{JOB} .vt-btn')].map(b => b.textContent.trim()),
      sideList: !!document.querySelector('#viewer-{JOB} .v-bar .v-bnd, #viewer-{JOB} .v-bar .vo-rows'),
      oldTools: !!document.querySelector('#viewer-{JOB} .v-tools'),
      open: [...document.querySelectorAll('#viewer-{JOB} .v-ov')].filter(o => !o.hidden).length}}))()""")
    assert seen["tools"] == ["View", "Section", "Parts", "Measure", "Snapshot"], seen
    assert not seen["sideList"] and not seen["oldTools"] and seen["open"] == 0, seen

    _click_el(live, _btn("view"))
    _click_el(live, _btn("parts"))
    state = live.evaluate(f"""(() => ({{current: {T}.current(),
      open: [...document.querySelectorAll('#viewer-{JOB} .v-ov')].filter(o => !o.hidden).map(o => o.dataset.tool),
      expanded: [...document.querySelectorAll('#viewer-{JOB} .vt-btn')].map(b => b.getAttribute('aria-expanded'))}}))()""")
    assert state["current"] == "parts" and state["open"] == ["parts"], state
    assert state["expanded"] == ["false", "false", "true", "false", "false"], state

    # Esc closes it and gives the focus back to its button
    live.press("Escape", code="Escape", vk=27)
    after = live.evaluate(f"({{current: {T}.current(), focus: document.activeElement.id}})")
    assert after == {"current": None, "focus": f"v-{JOB}-vt-parts"}, after

    # a click outside closes it; a drag on the part (a turn) does not
    _click_el(live, _btn("view"))
    assert live.evaluate(f"{T}.current()") == "view"
    cx, cy = live.evaluate(f"""(() => {{ const r = document.getElementById('v-canvas-{JOB}').getBoundingClientRect();
      return [r.left + r.width * 0.3, r.top + r.height * 0.8]; }})()""")
    send = live._send
    send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": cx, "y": cy, "button": "left", "buttons": 1, "clickCount": 1})
    send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": cx + 40, "y": cy, "button": "left", "buttons": 1})
    send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": cx + 40, "y": cy, "button": "left", "buttons": 0, "clickCount": 1})
    assert live.evaluate(f"{T}.current()") == "view", "a drag on the part closed the overlay"
    _click_el(live, f"document.querySelector('#viewer-{JOB} .v-bar .v-title')")
    assert live.evaluate(f"{T}.current()") is None
    assert_clean(live, "the viewer toolbar")


def test_view_sets_the_view_the_camera_and_the_axes(live, tmp_path):
    _ready(live, tmp_path)
    _click_el(live, _btn("view"))

    def look(key):
        live.evaluate(f"document.querySelector('#viewer-{JOB} .vo-item[data-view={key}]').click()")
        return live.evaluate(f"{T}.dir()")

    # with +Z up: Front looks along +Y, Top straight down, Side along -X
    assert look("front") == pytest.approx([0, 1, 0], abs=1e-6)
    assert look("top") == pytest.approx([0, 0, -1], abs=1e-6)
    assert live.evaluate(f"{V}.up()") == pytest.approx([0, 1, 0], abs=1e-6)
    assert look("side") == pytest.approx([-1, 0, 0], abs=1e-6)
    corner = look("corner")
    assert corner == pytest.approx([-x / math.hypot(1, 1, 0.7) for x in (1, -1, 0.7)], abs=1e-6)

    # Flat and back; the axes switch off and on
    got = live.evaluate(f"""(() => {{ const ov = document.querySelector('#viewer-{JOB} .v-ov-view');
      const seg = (n, v) => ov.querySelector(`.vo-seg[data-seg="${{n}}"] button[data-v="${{v}}"]`).click();
      const r = {{}};
      seg('camera', 'flat'); r.flat = {T}.parallel();
      seg('camera', 'persp'); r.persp = {T}.parallel();
      seg('axes', 'off'); r.off = {T}.axes();
      seg('axes', 'on'); r.on = {T}.axes();
      return r; }})()""")
    assert got == {"flat": True, "persp": False, "off": False, "on": True}, got

    # F fits from anywhere on the page that is not a text box: zoom out with the wheel, then F
    live.evaluate(f"{T}.close()")
    fit = live.evaluate(f"{V}.dist()")
    cx, cy = live.evaluate(f"""(() => {{ const r = document.getElementById('v-canvas-{JOB}').getBoundingClientRect();
      return [r.left + r.width / 2, r.top + r.height / 2]; }})()""")
    for _ in range(4):
        live._send("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": cx, "y": cy, "deltaX": 0, "deltaY": 240})
    live.settle(0.3)
    assert live.evaluate(f"{V}.dist()") > 1.1 * fit
    live.press("f", code="KeyF", vk=70, text="f")
    assert live.evaluate(f"{V}.dist()") == pytest.approx(fit, rel=0.05)
    assert_clean(live, "the View overlay")


def test_section_cuts_the_part_on_one_side_and_draws_the_cut(live, tmp_path):
    _ready(live, tmp_path)
    _click_el(live, _btn("section"))
    cut = live.evaluate(f"""(() => {{ const ov = document.querySelector('#viewer-{JOB} .v-ov-section');
      const r = {{before: {T}.cut()}};
      ov.querySelector('.vo-seg[data-seg="cut-axis"] button[data-v="y"]').click();
      r.y = {T}.cut();
      document.getElementById('v-cut-flip-{JOB}').click(); r.flip = {T}.cut();
      document.getElementById('v-cut-on-{JOB}').click(); r.off = {T}.cut();
      r.label = document.getElementById('v-cut-on-{JOB}').textContent;
      return r; }})()""")
    assert cut["before"]["on"] is False and set(cut["before"]["planes"]) == {0}, cut
    # one plane on every part, and the line where it cuts drawn on each part it crosses
    assert cut["y"]["on"] and cut["y"]["axis"] == "y" and set(cut["y"]["planes"]) == {1}, cut
    assert cut["y"]["lines"] == len(cut["y"]["planes"]), cut
    assert cut["flip"]["flip"] is True and cut["flip"]["on"], cut
    assert cut["off"]["on"] is False and set(cut["off"]["planes"]) == {0} and cut["off"]["lines"] == 0, cut
    assert cut["label"] == "Section off"

    # the slider moves the cut and turns it on
    live.evaluate(f"""(() => {{ const r = document.getElementById('v-cut-at-{JOB}');
      r.value = 20; r.dispatchEvent(new Event('input')); }})()""")
    live.wait_for(f"{T}.cut().on && Math.abs({T}.cut().at - 0.2) < 1e-9", timeout=5, what="the slider's cut")
    assert_clean(live, "the Section overlay")


def test_parts_lists_finds_hides_and_picks_the_parts(live, tmp_path):
    _ready(live, tmp_path)
    _click_el(live, _btn("parts"))
    rows = live.evaluate(f"[...document.querySelectorAll('#v-bnd-chips-{JOB} .vo-row')].map(r => r.dataset.label)")
    assert sorted(rows) == ["inlet", "outlet", "wall"], rows
    # the search box has the focus, and the list narrows as the user types
    assert live.evaluate("document.activeElement.id") == f"v-parts-q-{JOB}"
    found = live.evaluate(f"""(() => {{ const q = document.getElementById('v-parts-q-{JOB}');
      q.value = 'out'; q.dispatchEvent(new Event('input'));
      const shown = [...document.querySelectorAll('#v-bnd-chips-{JOB} .vo-row')].filter(r => !r.hidden).map(r => r.dataset.label);
      q.value = 'nothing-like-it'; q.dispatchEvent(new Event('input'));
      const none = document.querySelector('#v-bnd-chips-{JOB} .vo-empty');
      const r = {{shown, empty: !none.hidden, says: none.textContent}};
      q.value = ''; q.dispatchEvent(new Event('input'));
      return r; }})()""")
    assert found["shown"] == ["outlet"] and found["empty"], found
    assert "nothing-like-it" in found["says"], found

    # the eye hides a part and shows it again
    row = f"document.querySelector('#v-bnd-chips-{JOB} .vo-row[data-label=inlet]')"
    _click_el(live, f"{row}.querySelector('.vo-eye')")
    assert "inlet" not in live.evaluate(f"{T}.visible()")
    assert live.evaluate(f"{row}.querySelector('.vo-eye').getAttribute('aria-pressed')") == "false"
    _click_el(live, f"{row}.querySelector('.vo-eye')")
    assert "inlet" in live.evaluate(f"{T}.visible()")

    # a row picks its part: it turns crimson and a small chip at the bottom says what it is
    _click_el(live, f"document.querySelector('#v-bnd-chips-{JOB} .vo-row[data-label=outlet] .vo-pick')")
    chip = live.evaluate(f"""(() => {{ const c = document.getElementById('v-picked-{JOB}');
      return {{name: {T}.selected(), text: c.querySelector('.vp-t').textContent,
               colour: {T}.colourOf('outlet')}}; }})()""")
    assert chip["name"] == "outlet" and "1 face" in chip["text"], chip
    assert chip["colour"] == pytest.approx([0.886, 0.173, 0.173], abs=1e-6), chip
    _click_el(live, f"document.querySelector('#v-picked-{JOB} [data-a=iso]')")
    assert live.evaluate(f"{T}.visible()") == ["outlet"]
    _click_el(live, f"document.querySelector('#v-picked-{JOB} [data-a=clear]')")
    assert live.evaluate(f"{T}.selected()") is None
    assert sorted(live.evaluate(f"{T}.visible()")) == ["inlet", "outlet", "wall"]

    # Show: the surface style, and the colour the part is drawn in
    style = live.evaluate(f"""(() => {{ const ov = document.querySelector('#viewer-{JOB} .v-ov-parts');
      const seg = v => ov.querySelector(`.vo-seg[data-seg="surface"] button[data-v="${{v}}"]`).click();
      const r = {{start: {T}.surface()}};
      seg('wire'); r.wire = {T}.surface();
      seg('solid'); r.solid = {T}.surface();
      seg('edges'); r.edges = {T}.surface();
      const row = ov.querySelector('.vo-colour-row');
      r.colourRow = !row.hidden;
      row.querySelector('[data-colour=non_ortho]').click(); r.metric = window._vdbg['{JOB}:heat'].active();
      r.pressed = row.querySelector('[data-colour=non_ortho]').getAttribute('aria-pressed');
      row.querySelector('[data-colour=part]').click(); r.back = window._vdbg['{JOB}:heat'].active();
      return r; }})()""")
    assert style["start"] == {"mode": "edges", "edges": True, "rep": 2}, style
    assert style["wire"]["rep"] == 1 and style["solid"] == {"mode": "solid", "edges": False, "rep": 2}, style
    assert style["edges"]["edges"] is True, style
    assert style["colourRow"] and style["metric"] == "non_ortho" and style["pressed"] == "true", style
    assert style["back"] is None, style

    # a click on the part picks the part it lands on
    live.evaluate(f"{T}.close()")
    x, y = live.evaluate(f"{T}.screenOf([3, 0.5, 0.5])")
    live.click(x, y)
    live.wait_for(f"{T}.selected() === 'outlet'", timeout=5, what="a click on the outlet to pick it")
    assert_clean(live, "the Parts overlay")


def test_measure_gives_the_distance_between_two_clicked_points(live, tmp_path):
    _ready(live, tmp_path)
    _click_el(live, _btn("measure"))
    assert live.evaluate(f"{T}.measuring()") is True
    # two real clicks on the top of the row, 2 m apart; the overlay stays open between them
    for pt in ([0.5, 0.5, 1.0], [2.5, 0.5, 1.0]):
        x, y = live.evaluate(f"{T}.screenOf({pt})")
        live.click(x, y)
        live.settle(0.2)
    live.wait_for(f"{T}.measured().distance !== null", timeout=5, what="the second measuring click")
    m = live.evaluate(f"{T}.measured()")
    a, b = m["points"]
    assert a == pytest.approx([0.5, 0.5, 1.0], abs=0.02) and b == pytest.approx([2.5, 0.5, 1.0], abs=0.02), m
    assert m["distance"] == pytest.approx(2.0, abs=0.03) and m["label"].endswith(" m"), m
    shown = live.evaluate(f"""(() => ({{value: document.getElementById('v-meas-val-{JOB}').textContent,
      label: document.querySelector('#v-canvas-{JOB} .v-mlabel').textContent,
      line: !document.querySelector('#v-canvas-{JOB} .v-mline').hasAttribute('hidden'),
      open: {T}.current()}}))()""")
    assert shown["value"] == m["label"] == shown["label"] and shown["line"] and shown["open"] == "measure", shown

    # Esc ends measuring; the line stays until Clear
    live.press("Escape", code="Escape", vk=27)
    assert live.evaluate(f"{T}.measuring()") is False
    assert live.evaluate(f"{T}.measured().distance") is not None
    live.evaluate(f"document.getElementById('v-meas-clear-{JOB}').click()")
    assert live.evaluate(f"{T}.measured().points") == []
    assert_clean(live, "the Measure overlay")


def test_snapshot_saves_the_view_on_the_chosen_background_and_size(live, tmp_path):
    _ready(live, tmp_path)
    _click_el(live, _btn("snapshot"))
    # with no colour on the part there is no colour bar to add
    assert live.evaluate(f"document.querySelector('#viewer-{JOB} .vo-snap-bar').hidden") is True
    shots = live.evaluate(f"""(async () => {{ const t = {T}, r = {{}};
      r.white = await t.snapshot({{bg: 'white', scale: '2'}});
      r.clear = await t.snapshot({{bg: 'transparent', scale: '1'}});
      r.scene = await t.snapshot({{bg: 'scene', scale: '1'}});
      r.bg = t.background();
      return r; }})()""", timeout=60)
    w, h = shots["scene"]["canvas"]
    assert (shots["white"]["width"], shots["white"]["height"]) == (2 * w, 2 * h), shots
    assert shots["white"]["corner"] == [255, 255, 255, 255], shots
    assert shots["clear"]["corner"][3] == 0, shots
    assert shots["scene"]["corner"][:3] == pytest.approx([round(c * 255) for c in shots["bg"][:3]], abs=2), shots
    assert shots["scene"]["corner"][3] == 255, shots
    # the canvas is the same size after every picture
    assert live.evaluate(f"{V}.size()") == [w, h]

    # the colour bar row appears once the part is coloured by a figure
    live.evaluate(f"{T}.close(); window._vdbg['{JOB}:heat'].set('non_ortho')")
    _click_el(live, _btn("snapshot"))
    assert live.evaluate(f"document.querySelector('#viewer-{JOB} .vo-snap-bar').hidden") is False

    # Save picture hands the browser a PNG
    live.evaluate("""(() => { window.__saved = [];
      const real = HTMLAnchorElement.prototype.click;
      HTMLAnchorElement.prototype.click = function () {
        if (this.download && this.href.startsWith('blob:')) { window.__saved.push(this.download); return; }
        return real.call(this); }; })()""")
    _click_el(live, f"document.getElementById('v-snap-save-{JOB}')")
    live.wait_for("window.__saved.length === 1", timeout=30, what="the picture to be saved")
    assert live.evaluate("window.__saved") == [f"mesh_{JOB[:8]}.png"]
    assert_clean(live, "the Snapshot overlay")


def test_the_part_is_a_matte_mid_tone_grey_read_from_the_theme(live, tmp_path):
    _ready(live, tmp_path)
    got = live.evaluate(f"""(() => {{ const css = getComputedStyle(document.documentElement);
      return {{body: css.getPropertyValue('--part-body').trim(), canvas: css.getPropertyValue('--viewer-canvas').trim(),
              first: {T}.colourOf(document.querySelector('#v-bnd-chips-{JOB} .vo-row').dataset.label),
              material: {T}.material(), bg: {T}.background()}}; }})()""")
    assert got["body"] == "#86857c", got
    # the first patch wears the body grey; the material is matte
    assert got["first"] == pytest.approx([0x86 / 255, 0x85 / 255, 0x7c / 255], abs=1e-3), got
    assert got["material"]["ambient"] == pytest.approx(0.22) and got["material"]["specular"] == pytest.approx(0.06), got
    # the scene is the theme's viewer canvas
    assert got["canvas"] == "#050505" and got["bg"][:3] == pytest.approx([0x05 / 255, 0x05 / 255, 0x05 / 255], abs=1e-3), got
    assert_clean(live, "the part's colours")
