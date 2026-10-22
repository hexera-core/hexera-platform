# Responsibility: Verify the 3D navigation every view shares - the ViewCube under real clicks, the eased
# view changes, a click that selects the part and a second that lets it go, touch zoom, and a page that
# never stalls however fast the cube is clicked - in a real browser.
# Boundaries: the skin payload is the one the worker stores; only the network is stood in for.
from __future__ import annotations

import math

import pytest
from conftest import assert_clean
from test_geometry_stage_external import SESSION, _check, _open_stage

pytestmark = pytest.mark.ui

H = f"window._vdbg['gstage:{SESSION}']"
ROOT = f"document.getElementById('gs-canvas-{SESSION}')"


def _angle(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b)) / (math.hypot(*a) * math.hypot(*b))
    return math.degrees(math.acos(max(-1.0, min(1.0, dot))))


def _dir(live) -> list[float]:
    return live.evaluate(f"{H}.cam().getDirectionOfProjection().slice()")


def _open(live) -> None:
    # an earlier test in the same browser may have stored another Up axis for this session
    live.evaluate(f"localStorage.removeItem('hexera.view-up.session.{SESSION}'); 0")
    check = _check()
    check["proposal"]["flow"] = "internal"
    _open_stage(live, check)


def _real_click(live, selector: str) -> None:
    """A click the browser makes - pointerdown, pointerup and click on whatever is on top there -
    the way a mouse does it. A synthetic .click() would skip the hit test, and that hit test is what
    the canvas once stole every cube click with. A cube cell is named by its view direction ("0,0,1"
    is the TOP face's centre) and clicked where the cube shows it."""
    if selector[0].isdigit() or selector[0] == "-":
        x, y = live.evaluate(f"{H}.nav().cellPoint('{selector}')")
    else:
        x, y = live.evaluate(f"""(() => {{ const r = document.querySelector('{selector}').getBoundingClientRect();
          return [r.left + r.width / 2, r.top + r.height / 2]; }})()""")
    for t, buttons in (("mouseMoved", 0), ("mousePressed", 1), ("mouseReleased", 0)):
        live._send("Input.dispatchMouseEvent", {"type": t, "x": x, "y": y, "button": "left",
                                                "buttons": buttons, "clickCount": 1})


def _idle(live) -> None:
    live.wait_for(f"!{H}.nav().animating()", timeout=5, what="the turn to finish")


def test_real_clicks_on_the_cube_turn_the_view_smoothly(live):
    _open(live)
    ui = live.evaluate(f"""(() => {{ const r = {ROOT};
      return {{faces: r.querySelectorAll('.nav-face').length, cells: r.querySelectorAll('.nav-cell').length,
               toolbar: !!r.querySelector('.nav-bar'), arrows: r.querySelector('.nav-arrows').hidden}}; }})()""")
    assert ui == {"faces": 6, "cells": 54, "toolbar": False, "arrows": True}, ui

    # the TOP face, clicked as a mouse clicks it: the view turns there over a moment - never a jump
    start = _dir(live)
    top_key = live.evaluate(f"{ROOT}.querySelector('.nav-face[data-face=TOP] .nav-cell.centre').dataset.dir")
    assert top_key == "0,0,1", top_key
    _real_click(live, top_key)
    live.settle(0.15)
    midway = _dir(live)
    _idle(live)
    top = _dir(live)
    assert _angle(top, [0, 0, -1]) < 0.5, top
    assert 1.0 < _angle(start, midway) < _angle(start, top) - 1.0, (start, midway, top)

    # looking straight at a face shows the arrows; the right arrow steps a quarter turn
    assert live.evaluate(f"{ROOT}.querySelector('.nav-arrows').hidden") is False
    _real_click(live, ".nav-arr.right")
    _idle(live)
    side = _dir(live)
    assert abs(_angle(top, side) - 90) < 0.5, (top, side)

    # a roll keeps the direction and turns the picture a quarter
    up0 = live.evaluate(f"{H}.cam().getViewUp().slice()")
    _real_click(live, ".nav-roll.cw")
    _idle(live)
    assert _angle(side, _dir(live)) < 0.5
    assert abs(_angle(up0, live.evaluate(f"{H}.cam().getViewUp().slice()")) - 90) < 0.5

    # HOME goes back to the opening view
    _real_click(live, ".nav-home")
    _idle(live)
    assert _angle(_dir(live), start) < 0.5
    assert_clean(live, "the ViewCube")


def test_rapid_cube_clicks_never_stall_the_page(live):
    # Every face, edge and corner the cube shows, clicked as fast as a user can, the Up axis
    # flipped half way through: the main thread never blocks for 200 ms, and the camera stays a
    # camera (no NaN) wherever it ends.
    _open(live)
    live.evaluate("window.__long = []; new PerformanceObserver((l) => l.getEntries()"
                  ".forEach((e) => window.__long.push(Math.round(e.duration)))).observe({type: 'longtask'}); 0")
    for i in range(40):
        visible = live.evaluate(f"""(() => [...new Set([...{ROOT}.querySelectorAll('.nav-cell')].map((c) => c.dataset.dir))]
          .filter((k) => {H}.nav().cellPoint(k)))()""")
        assert visible, "no cube cell can be clicked"
        _real_click(live, visible[i % len(visible)])
        if i == 20:
            live.evaluate(f"""(() => {{ const s = document.getElementById('gs-up-{SESSION}');
              s.value = '-x'; s.dispatchEvent(new Event('change')); }})()""")
    _idle(live)
    live.settle(0.3)
    out = live.evaluate(f"""(() => ({{long: window.__long, pos: {H}.cam().getPosition().slice(),
                                      up: {H}.cam().getViewUp().slice()}}))()""")
    assert max(out["long"], default=0) < 200, out["long"]
    assert all(math.isfinite(v) for v in out["pos"] + out["up"]), out


def test_a_click_selects_the_part_a_second_click_lets_it_go(live):
    _open(live)
    cx, cy, ex, ey = live.evaluate(f"""(() => {{ const r = {ROOT}.getBoundingClientRect();
      return [r.left + r.width / 2, r.top + r.height / 2, r.left + 30, r.top + r.height - 30]; }})()""")

    def click(x, y):
        for t in ("mousePressed", "mouseReleased"):
            live._send("Input.dispatchMouseEvent", {"type": t, "x": x, "y": y, "button": "left",
                                                    "buttons": 1 if t == "mousePressed" else 0, "clickCount": 1})
        live.settle(0.2)

    click(cx, cy)
    assert live.evaluate(f"{H}.picked()") is True
    click(cx, cy)
    assert live.evaluate(f"{H}.picked()") is False, "a second click on the part lets it go"
    click(cx, cy)
    click(ex, ey)
    assert live.evaluate(f"{H}.picked()") is False, "a click on empty space lets it go"
    assert_clean(live, "selecting the part")


def test_two_fingers_pinch_to_zoom(live):
    _open(live)
    out = live.evaluate(f"""(async () => {{
      const host = {ROOT}, r = host.getBoundingClientRect();
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
