# Responsibility: Verify the geometry stage wears the mesh viewer's toolbar - View, Measure and
# Snapshot on the CAD part, View and Snapshot on a thermal model with "See inside the case" in
# View - and that the part is the theme's matte mid-tone grey on the theme's canvas.
# Boundaries: the skins are built through the worker's own code from a tube and an ECXML model
# made here; only the network is stood in for. It asserts the scene and the page, not pixels.
from __future__ import annotations

import pytest
from conftest import assert_clean
from test_geometry_stage_open_ends import R_IN, R_OUT, _check, _holes, _open, _skin, _tube
from test_thermal_panel import SESSION as THERMAL
from test_thermal_panel import _open as _open_thermal
from test_thermal_panel import served  # noqa: F401 - the module fixture the thermal test uses

pytestmark = pytest.mark.ui


def test_the_cad_stage_has_view_measure_and_snapshot_and_a_mid_tone_part(live):
    sid = "stagetools"
    tris = _tube(R_OUT, R_IN)
    _open(live, sid, _skin(tris), _check(holes=_holes(tris)))
    H = f"window._vdbg['gstage:{sid}']"
    seen = live.evaluate(f"""(() => {{ const root = document.getElementById('gstage-{sid}');
      return {{tools: [...root.querySelectorAll('.vt-btn')].map(b => b.textContent.trim()),
               old: !!root.querySelector('.v-tools'), ruler: !!root.querySelector('.gc-measure'),
               css: getComputedStyle(document.documentElement).getPropertyValue('--part-body').trim(),
               material: {H}.material(), bg: {H}.background()}}; }})()""")
    assert seen["tools"] == ["View", "Measure", "Snapshot"], seen
    assert not seen["old"] and not seen["ruler"], seen
    # the part: the theme's mid-tone grey, little ambient, little shine, on the theme's canvas
    assert seen["css"] == "#7c828a", seen
    assert seen["material"]["color"] == pytest.approx([0x7c / 255, 0x82 / 255, 0x8a / 255], abs=1e-3), seen
    assert seen["material"]["ambient"] == pytest.approx(0.22) and seen["material"]["specular"] == pytest.approx(0.06), seen
    assert seen["bg"][:3] == pytest.approx([0x0d / 255, 0x0d / 255, 0x0c / 255], abs=1e-3), seen

    # VIEW: the set views turn the camera; the Up select and Fit live in the overlay; Esc closes it
    view = live.evaluate(f"""(() => {{ const h = {H};
      document.getElementById('gs-{sid}-vt-view').click();
      const pick = k => {{ document.querySelector('#gstage-{sid} .vo-item[data-view=' + k + ']').click(); return h.dir(); }};
      return {{open: h.tools.current(), front: pick('front'), top: pick('top'),
               inOverlay: !!document.querySelector('#gstage-{sid} .v-ov-view #gs-up-{sid}')
                 && !!document.querySelector('#gstage-{sid} .v-ov-view #gs-fit-{sid}')}}; }})()""")
    assert view["open"] == "view" and view["inOverlay"], view
    assert view["front"] == pytest.approx([0, 1, 0], abs=1e-6) and view["top"] == pytest.approx([0, 0, -1], abs=1e-6), view
    live.press("Escape", code="Escape", vk=27)
    assert live.evaluate(f"{H}.tools.current()") is None

    # MEASURE: opening it arms the ruler; closing it ends the ruler
    live.evaluate(f"document.getElementById('gs-{sid}-vt-measure').click()")
    assert live.evaluate(f"{H}.measuring()") is True
    live.evaluate(f"document.getElementById('gs-{sid}-vt-measure').click()")
    assert live.evaluate(f"{H}.measuring()") is False

    # SNAPSHOT: 2x on white, and the canvas the same size after it
    shot = live.evaluate(f"""(async () => {{ const h = {H};
      const r = await h.tools.snapshot({{bg: 'white', scale: '2'}}); return r; }})()""", timeout=60)
    w, h = shot["canvas"]
    assert (shot["width"], shot["height"]) == (2 * w, 2 * h) and shot["corner"] == [255, 255, 255, 255], shot
    live.evaluate(f"window.__stages['{sid}'].release()")
    assert_clean(live, "the geometry stage's toolbar")


def test_a_thermal_model_sees_inside_its_case_from_the_view_overlay(live, served):  # noqa: F811
    _open_thermal(live, served)
    H = f"window._vdbg['gstage:{THERMAL}']"
    got = live.evaluate(f"""(() => {{ const h = {H}, root = document.getElementById('gstage-{THERMAL}');
      const seg = v => root.querySelector('.v-ov-view .vo-seg[data-seg=xray] button[data-v=' + v + ']');
      const r = {{tools: [...root.querySelectorAll('.vt-btn')].map(b => b.textContent.trim()), start: h.xray()}};
      seg('off').click(); r.off = h.xray(); r.caseOff = h.opacity('Case');
      seg('on').click(); r.on = h.xray(); r.caseOn = h.opacity('Case');
      r.showAll = !!document.getElementById('gs-all-{THERMAL}');
      return r; }})()""")
    assert got["tools"] == ["View", "Snapshot"], got
    assert got["start"] is True and got["off"] is False and got["on"] is True, got
    assert got["caseOff"] > got["caseOn"], got
    assert got["showAll"], got
    assert_clean(live, "the thermal stage's toolbar")
