# Responsibility: Verify the 3D navigation every view shares - the ViewCube, the eased view changes, the
# toolbar's tools, the section, and a click that selects the part - in a real browser.
# Boundaries: the skin payload is the one the worker stores; only the network is stood in for.
from __future__ import annotations

import math

import pytest
from conftest import assert_clean
from test_geometry_stage_external import SESSION, _check, _open_stage

pytestmark = pytest.mark.ui

H = f"window._vdbg['gstage:{SESSION}']"


def _angle(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b)) / (math.hypot(*a) * math.hypot(*b))
    return math.degrees(math.acos(max(-1.0, min(1.0, dot))))


def _dir(live) -> list[float]:
    return live.evaluate(f"{H}.cam().getDirectionOfProjection().slice()")


def test_the_cube_turns_the_view_smoothly_and_the_arrows_step_round_it(live):
    check = _check()
    check["proposal"]["flow"] = "internal"
    _open_stage(live, check)
    root = f"document.getElementById('gs-canvas-{SESSION}')"

    ui = live.evaluate(f"""(() => {{
      const r = {root};
      return {{cube: !!r.querySelector('.nav-cube'), faces: r.querySelectorAll('.nav-face').length,
               cells: r.querySelectorAll('.nav-cell').length,
               tools: [...r.querySelectorAll('.nav-bar .nav-btn span')].map(s => s.textContent),
               fitInBar: !!r.querySelector('.nav-bar #gs-fit-{SESSION}'),
               orbit: r.querySelector('[data-nav=orbit]').getAttribute('aria-pressed'),
               arrows: r.querySelector('.nav-arrows').hidden}};
    }})()""")
    assert ui["cube"] and ui["faces"] == 6 and ui["cells"] == 54, ui
    assert ui["tools"] == ["Home", "Fit", "Pan", "Zoom", "Orbit", "Section"], ui
    assert ui["fitInBar"] is True and ui["orbit"] == "true", ui
    assert ui["arrows"] is True, "the three-quarter view is not looking straight at a face"

    # A CLICK ON THE TOP FACE turns the view there over a moment - never a jump
    start = _dir(live)
    live.evaluate(f"{root}.querySelector('.nav-face[data-face=TOP] .nav-cell.centre').click()")
    live.settle(0.15)
    midway = _dir(live)
    live.wait_for(f"!{H}.nav().animating()", timeout=5, what="the turn to the top view to finish")
    top = _dir(live)
    assert _angle(top, [0, 0, -1]) < 0.5, top
    assert 1.0 < _angle(start, midway) < _angle(start, top) - 1.0, (start, midway, top)
    assert live.evaluate(f"{root}.querySelector('.nav-arrows').hidden") is False, \
        "looking straight at a face shows the arrows"

    # the right arrow steps to the face beside it: a quarter turn
    live.evaluate(f"{root}.querySelector('.nav-arr.right').click()")
    live.wait_for(f"!{H}.nav().animating()", timeout=5, what="the step to finish")
    side = _dir(live)
    assert abs(_angle(top, side) - 90) < 0.5, (top, side)

    # a roll keeps the direction and turns the picture a quarter
    up0 = live.evaluate(f"{H}.cam().getViewUp().slice()")
    live.evaluate(f"{root}.querySelector('.nav-roll.cw').click()")
    live.wait_for(f"!{H}.nav().animating()", timeout=5, what="the roll to finish")
    assert _angle(side, _dir(live)) < 0.5
    assert abs(_angle(up0, live.evaluate(f"{H}.cam().getViewUp().slice()")) - 90) < 0.5

    # HOME goes back to the opening view
    live.evaluate(f"{root}.querySelector('.nav-home').click()")
    live.wait_for(f"!{H}.nav().animating()", timeout=5, what="the way home to finish")
    assert _angle(_dir(live), start) < 0.5
    assert_clean(live, "the ViewCube")


def test_tools_section_and_a_click_that_selects_the_part(live):
    check = _check()
    check["proposal"]["flow"] = "internal"
    _open_stage(live, check)
    root = f"document.getElementById('gs-canvas-{SESSION}')"

    tools = live.evaluate(f"""(() => {{
      const r = {root}, p = (t) => r.querySelector('[data-nav=' + t + ']').getAttribute('aria-pressed');
      r.querySelector('[data-nav=pan]').click();
      const pan = {{pan: p('pan'), orbit: p('orbit'), tool: {H}.nav().tool()}};
      r.querySelector('[data-nav=orbit]').click();
      r.querySelector('[data-nav=section]').click();
      const sec = {{on: {H}.nav().section(), panel: !r.querySelector('.nav-section').hidden}};
      r.querySelector('[data-nav=section]').click();
      return {{pan, sec, off: {H}.nav().section()}};
    }})()""")
    assert tools["pan"] == {"pan": "true", "orbit": "false", "tool": "pan"}, tools
    assert tools["sec"] == {"on": True, "panel": True} and tools["off"] is False, tools

    # a real click on the middle of the part selects it; a click on empty canvas clears it
    cx, cy, ex, ey = live.evaluate(f"""(() => {{
      const r = {root}.getBoundingClientRect();
      return [r.left + r.width / 2, r.top + r.height / 2, r.left + 30, r.top + r.height - 30]; }})()""")

    def click(x, y):
        for t in ("mousePressed", "mouseReleased"):
            live._send("Input.dispatchMouseEvent", {"type": t, "x": x, "y": y, "button": "left",
                                                    "buttons": 1 if t == "mousePressed" else 0, "clickCount": 1})
        live.settle(0.2)

    click(cx, cy)
    assert live.evaluate(f"{H}.picked()") is True
    click(ex, ey)
    assert live.evaluate(f"{H}.picked()") is False
    assert_clean(live, "the toolbar, the section and selection")


def test_two_fingers_pinch_to_zoom(live):
    check = _check()
    check["proposal"]["flow"] = "internal"
    _open_stage(live, check)
    out = live.evaluate(f"""(async () => {{
      const host = document.getElementById('gs-canvas-{SESSION}'), r = host.getBoundingClientRect();
      const cam = {H}.cam(), dist = () => {{ const f = cam.getFocalPoint(), p = cam.getPosition();
        return Math.hypot(p[0] - f[0], p[1] - f[1], p[2] - f[2]); }};
      const cx = r.left + r.width / 2, cy = r.top + r.height / 2, ev = (t, id, x) =>
        host.dispatchEvent(new PointerEvent(t, {{pointerId: id, pointerType: 'touch', clientX: x, clientY: cy,
                                                 bubbles: true, isPrimary: id === 1}}));
      const before = dist();
      ev('pointerdown', 1, cx - 20); ev('pointerdown', 2, cx + 20);
      for (let k = 1; k <= 10; k++) {{ ev('pointermove', 1, cx - 20 - 6 * k); ev('pointermove', 2, cx + 20 + 6 * k); }}
      ev('pointerup', 1, cx - 80); ev('pointerup', 2, cx + 80);
      await new Promise(res => setTimeout(res, 100));
      return {{before, after: dist()}};
    }})()""")
    # the fingers moved from 40 px apart to 160: the part is shown four times larger
    assert out["after"] == pytest.approx(out["before"] / 4, rel=0.05), out
