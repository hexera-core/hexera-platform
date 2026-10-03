# Responsibility: Verify the geometry stage's ruler: with "Measure" on, two clicks on the part draw
# a line with the distance in the file's unit; a click within a few pixels of a hole's edge lands
# on the edge, so a bore's width across is two clicks; a third click starts again; Esc ends it;
# and nothing of it reaches the confirmation.
# Boundaries: the skin and the holes are built through the worker's own code from a tube made
# here; the clicks are the browser's own mouse, through the camera. Only the network is stood in for.
from __future__ import annotations

import pytest
from conftest import assert_clean
from test_geometry_stage_open_ends import ORIGIN, R_IN, R_OUT, _check, _holes, _open, _skin, _tube

pytestmark = pytest.mark.ui


def test_two_clicks_measure_the_part_and_a_holes_edge_pulls_the_click_onto_it(live):
    sid = "measure"
    tris = _tube(R_OUT, R_IN)
    _open(live, sid, _skin(tris), _check(holes=_holes(tris)))
    ox, oy, oz = ORIGIN
    # the camera looks straight into the x = 0 end, 300 mm out; Measure on
    live.evaluate(f"""(() => {{
      const h = window._vdbg['gstage:{sid}'], root = document.getElementById('gstage-{sid}');
      const cam = h.cam();
      cam.setFocalPoint({ox}, {oy}, {oz}); cam.setPosition({ox - 0.3}, {oy}, {oz}); cam.setViewUp(0, 0, 1);
      h.render();
      root.querySelector('.gc-measure').click();
    }})()""")
    centre = live.evaluate(f"""(() => {{ const r = document.getElementById('gs-canvas-{sid}').getBoundingClientRect();
      return [r.left + r.width / 2, r.top + r.height / 2]; }})()""")
    assert live.evaluate(f"window._vdbg['gstage:{sid}'].measuring()") is True

    def screen(p):
        return live.evaluate(f"window._vdbg['gstage:{sid}'].screenOf({list(p)})")

    def state():
        return live.evaluate(f"""(() => {{ const h = window._vdbg['gstage:{sid}'], m = h.measured();
          const el = document.querySelector('#gstage-{sid} .gc-mlabel');
          return {{n: m.points.length, mm: m.mm, label: m.label, shown: el ? el.textContent : null,
                   measuring: h.measuring(), rows: document.querySelectorAll('#gstage-{sid} .gc-table tbody tr').length}}; }})()""")

    # ACROSS THE BORE: two clicks a few pixels off its edge, on opposite sides, land on the edge
    a, b = screen((ox, oy + R_IN, oz)), screen((ox, oy - R_IN, oz))
    live.click(a[0] + 3, a[1] - 2)
    live.click(b[0] - 2, b[1] + 3)
    across = state()
    assert across["n"] == 2 and across["mm"] == pytest.approx(80.0, abs=0.01), across
    assert across["label"] == "80 mm" and across["shown"] == "80 mm", across

    # A THIRD CLICK STARTS AGAIN: on the end face, well clear of either edge, then its opposite
    c, d = screen((ox, oy, oz + 0.045)), screen((ox, oy, oz - 0.045))
    live.click(*c)
    again = state()
    assert again["n"] == 1 and again["label"] is None and again["shown"] is None, again
    live.click(*d)
    face = state()
    assert face["n"] == 2 and face["mm"] == pytest.approx(90.0, abs=0.5) and face["label"].endswith(" mm"), face

    # ESC ENDS IT: no line, no label, the button off - and nothing was added or sent
    live.press("Escape", vk=27)
    done = state()
    assert done == {"n": 0, "mm": None, "label": None, "shown": None, "measuring": False, "rows": 0}, done
    assert not live.evaluate(f"document.querySelector('#gstage-{sid} .gc-measure').classList.contains('armed')")
    assert live.evaluate(f"(window.__confirmed || {{}})['{sid}'] || null") is None
    live.evaluate(f"window.__stages['{sid}'].release()")
    assert centre
    assert_clean(live, "measuring on the geometry stage")
