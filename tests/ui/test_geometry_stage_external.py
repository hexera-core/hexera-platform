# Responsibility: Verify a body in a flow opens on the stage with the wind arrow and the far-field
# box, that the form's external answers redraw them, and that proceeding sends the axis, the
# reference length, the margins and the ground flag.
# Boundaries: the skin payload is the one the worker stores; only the network is stood in for.
from __future__ import annotations

import json

import pytest
from conftest import assert_clean

pytestmark = pytest.mark.ui

SESSION = "stage-external"


def _skin() -> dict:
    from meshpipeline.cad.stl_io import _box_triangles
    from meshpipeline.render.viewer_pack import stl_response
    resp = stl_response({"skin": _box_triangles((0.0, 0.0, 0.0), (4.2, 1.8, 1.4))}, {}, "m")
    resp.update(is_mesh=False, cell_count=0)
    return resp


def _check() -> dict:
    return {"status": "ready", "named": True, "skin": True, "pictures": [], "proposal": {
        "part": "a car body", "input_kind": "solid-body", "flow": "external", "openings": [],
        "size_mm": [4200.0, 1800.0, 1400.0], "seed_point_mm": None, "notes": [],
        "flow_axis": "+x", "flow_axis_guessed": True, "reference_length_mm": 4200.0,
        "extents": {"upstream": 5, "downstream": 10, "lateral": 5, "vertical": 5}, "grounded": True}}


def test_a_body_in_a_flow_shows_the_arrow_and_the_box_and_confirms_the_far_field(live):
    live.evaluate("""(async () => {
      const SKIN = __SKIN__, CHECK = __CHECK__;
      const real = window.fetch.bind(window);
      window.fetch = (u, o) => String(u).includes('/geometry/__S__/check/skin')
        ? Promise.resolve(new Response(JSON.stringify(SKIN), {status: 200, headers: {'Content-Type': 'application/json'}}))
        : real(u, o);
      window.__confirmed = null;
      const { openGeometryStage } = await import('/static/js/viewer/geometry_stage.js');
      await openGeometryStage('__S__', CHECK,
        async (body) => { window.__confirmed = body; return {message: 'ok'}; },
        {anchorEl: document.getElementById('stage'), fallback: () => { window.__fellBack = true; }});
    })()""".replace("__SKIN__", json.dumps(_skin())).replace("__CHECK__", json.dumps(_check()))
       .replace("__S__", SESSION), timeout=60)
    live.wait_for(f"window._vdbg && window._vdbg['gstage:{SESSION}']", timeout=90,
                  what="the geometry stage to initialise")

    opened = live.evaluate(f"""(() => {{
      const h = window._vdbg['gstage:{SESSION}'];
      const root = document.getElementById('gstage-{SESSION}');
      return {{pins: h.pins(), ext: h.external(), fellBack: !!window.__fellBack,
               intHidden: root.querySelector('.gc-int').hidden, extHidden: root.querySelector('.gc-ext').hidden,
               axis: root.querySelector('.gc-axis').value, guess: !!root.querySelector('.gc-guess'),
               ref: root.querySelector('.gc-ref').value, ground: root.querySelector('.gc-ground').checked}};
    }})()""")
    assert opened["fellBack"] is False and opened["pins"] == 0, opened
    assert opened["ext"]["arrow"] is True and opened["ext"]["box"] is True, opened
    assert opened["intHidden"] is True and opened["extHidden"] is False, opened
    assert opened["axis"] == "+x" and opened["guess"] is True and opened["ref"] == "4200" and opened["ground"] is True

    # turning the flow to -y and lifting the part off the ground redraws the arrow and the box
    redrawn = live.evaluate(f"""(() => {{
      const h = window._vdbg['gstage:{SESSION}'];
      const root = document.getElementById('gstage-{SESSION}');
      const before = h.external().actors;
      const axis = root.querySelector('.gc-axis'); axis.value = '-y'; axis.dispatchEvent(new Event('change', {{bubbles: true}}));
      const g = root.querySelector('.gc-ground'); g.checked = false; g.dispatchEvent(new Event('change', {{bubbles: true}}));
      const up = root.querySelector('.gc-ext-upstream'); up.value = '3'; up.dispatchEvent(new Event('input', {{bubbles: true}}));
      return {{before, after: h.external().actors, ext: h.external()}};
    }})()""")
    assert redrawn["before"] == 3 and redrawn["after"] == 2, redrawn     # the ground plate went with the flag
    assert redrawn["ext"]["arrow"] is True and redrawn["ext"]["box"] is True

    live.evaluate(f"""(() => {{ document.querySelector('#gstage-{SESSION} .gc-proceed').click(); }})()""")
    live.wait_for("window.__confirmed !== null", timeout=30, what="the confirmation to be sent")
    body = live.evaluate("window.__confirmed")
    assert body["flow"] == "external" and body["input_kind"] == "solid-body" and body["openings"] == []
    assert body["flow_axis"] == "-y" and body["reference_length_mm"] == 4200.0 and body["grounded"] is False
    assert body["extents"] == {"upstream": 3, "downstream": 10, "lateral": 5, "vertical": 5}
    assert live.evaluate(f"!document.getElementById('gstage-{SESSION}')") is True
    assert_clean(live, "the external geometry stage")


def test_switching_the_flow_swaps_the_table_for_the_far_field_on_the_card(live):
    # the card is the stage's fallback and shares the form: the same switch must work there
    check = _check(); check["proposal"]["flow"] = "internal"; check["skin"] = False
    live.evaluate("""(async () => {
      const CHECK = __CHECK__;
      const { Stage } = await import('/static/js/render/stage.js');
      Stage.geometryCheck(CHECK, async () => ({message: 'ok'}));
    })()""".replace("__CHECK__", json.dumps(check)), timeout=60)
    live.wait_for("!!document.querySelector('.gc-card')", timeout=30, what="the card to render")
    state = live.evaluate("""(() => {
      const card = document.querySelector('.gc-card');
      const before = {int: card.querySelector('.gc-int').hidden, ext: card.querySelector('.gc-ext').hidden};
      const flow = card.querySelector('.gc-flow'); flow.value = 'external'; flow.dispatchEvent(new Event('change', {bubbles: true}));
      return {before, after: {int: card.querySelector('.gc-int').hidden, ext: card.querySelector('.gc-ext').hidden}};
    })()""")
    assert state["before"] == {"int": False, "ext": True} and state["after"] == {"int": True, "ext": False}, state
