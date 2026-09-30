# Responsibility: Verify a body in a flow opens on the stage with the wind arrow and the far-field
# box, that the form's external answers redraw them, that proceeding sends the axis, the
# reference length, the margins and the ground flag - and that the reference length follows the
# flow axis the user turns to, unless they typed their own, on the stage and on the card alike.
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
        # a fraction on purpose: an untouched reference length is confirmed exactly as served
        "flow_axis": "+x", "flow_axis_guessed": True, "reference_length_mm": 4200.6,
        "extents": {"upstream": 5, "downstream": 10, "lateral": 5, "vertical": 5}, "grounded": True}}


def _open_stage(live, check: dict) -> None:
    live.evaluate("""(async () => {
      const SKIN = __SKIN__, CHECK = __CHECK__;
      const real = window.fetch.bind(window);
      window.fetch = (u, o) => String(u).includes('/geometry/__S__/check/skin')
        ? Promise.resolve(new Response(JSON.stringify(SKIN), {status: 200, headers: {'Content-Type': 'application/json'}}))
        : real(u, o);
      window.__confirmed = null;
      const { openGeometryStage } = await import('/static/js/viewer/geometry_stage.js');
      window.__stage = await openGeometryStage('__S__', CHECK,
        async (body) => { window.__confirmed = body; return {message: 'ok'}; },
        {anchorEl: document.getElementById('stage'), fallback: () => { window.__fellBack = true; }});
    })()""".replace("__SKIN__", json.dumps(_skin())).replace("__CHECK__", json.dumps(check))
       .replace("__S__", SESSION), timeout=60)
    live.wait_for(f"window._vdbg && window._vdbg['gstage:{SESSION}']", timeout=90,
                  what="the geometry stage to initialise")


def _proceed(live) -> dict:
    live.evaluate(f"""(() => {{ document.querySelector('#gstage-{SESSION} .gc-proceed').click(); }})()""")
    live.wait_for("window.__confirmed !== null", timeout=30, what="the confirmation to be sent")
    return live.evaluate("window.__confirmed")


def test_a_body_in_a_flow_shows_the_arrow_and_the_box_and_confirms_the_far_field(live):
    _open_stage(live, _check())

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
    assert opened["axis"] == "+x" and opened["guess"] is True and opened["ref"] == "4200.6" and opened["ground"] is True

    # turning the flow to -x is the same line: the length stands as served. Turning it on to -y
    # makes the reference length the part's length along y, and lifting the part off the ground
    # redraws the arrow and the box
    redrawn = live.evaluate(f"""(() => {{
      const h = window._vdbg['gstage:{SESSION}'];
      const root = document.getElementById('gstage-{SESSION}');
      const before = h.external().actors;
      const axis = root.querySelector('.gc-axis');
      axis.value = '-x'; axis.dispatchEvent(new Event('change', {{bubbles: true}}));
      const flipped = root.querySelector('.gc-ref').value;
      axis.value = '-y'; axis.dispatchEvent(new Event('change', {{bubbles: true}}));
      const turned = root.querySelector('.gc-ref').value;
      const g = root.querySelector('.gc-ground'); g.checked = false; g.dispatchEvent(new Event('change', {{bubbles: true}}));
      const up = root.querySelector('.gc-ext-upstream'); up.value = '3'; up.dispatchEvent(new Event('input', {{bubbles: true}}));
      const lat = root.querySelector('.gc-ext-lateral'); lat.value = ''; lat.dispatchEvent(new Event('input', {{bubbles: true}}));
      const top = root.querySelector('.gc-ext-vertical'); top.value = '0'; top.dispatchEvent(new Event('input', {{bubbles: true}}));
      return {{before, after: h.external().actors, ext: h.external(), flipped, turned}};
    }})()""")
    assert redrawn["before"] == 3 and redrawn["after"] == 2, redrawn     # the ground plate went with the flag
    assert redrawn["ext"]["arrow"] is True and redrawn["ext"]["box"] is True
    assert redrawn["flipped"] == "4200.6" and redrawn["turned"] == "1800", redrawn

    body = _proceed(live)
    assert body["flow"] == "external" and body["input_kind"] == "solid-body" and body["openings"] == []
    assert body["flow_axis"] == "-y" and body["reference_length_mm"] == 1800 and body["grounded"] is False
    assert body["reference_length_typed"] is False
    # a blank box keeps its default and a zero is held to half a length: never a zero margin
    assert body["extents"] == {"upstream": 3, "downstream": 10, "lateral": 5, "vertical": 0.5}
    assert live.evaluate(f"!document.getElementById('gstage-{SESSION}')") is True
    assert_clean(live, "the external geometry stage")


def test_the_reference_length_follows_the_turned_axis_through_the_models_labels(live):
    """The demo gate: the NASA CRM read along +y got its 30.4 m span as the reference length, and
    the user's turn to +x kept it - a far field half the size. The length follows the axis, and
    the model's labels arriving afterwards (the form is drawn again) keep the user's axis and
    the length along it."""
    check = _check()
    check["proposal"].update(flow_axis="+y", reference_length_mm=1800.0)       # the wrong guess
    _open_stage(live, check)
    state = live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{SESSION}');
      const axis = root.querySelector('.gc-axis');
      axis.value = '+x'; axis.dispatchEvent(new Event('change', {{bubbles: true}}));
      const turned = root.querySelector('.gc-ref').value;
      // the model's labels land with its own guess: the user's axis and its length stand
      window.__stage.update({json.dumps(check)});
      const again = document.getElementById('gstage-{SESSION}');
      return {{turned, axis: again.querySelector('.gc-axis').value, ref: again.querySelector('.gc-ref').value}};
    }})()""")
    assert state == {"turned": "4200", "axis": "+x", "ref": "4200"}, state
    body = _proceed(live)
    assert body["flow_axis"] == "+x" and body["reference_length_mm"] == 4200 and body["reference_length_typed"] is False
    assert_clean(live, "the geometry stage with a turned axis")


def test_a_reference_length_the_user_typed_stays_theirs_when_the_axis_turns(live):
    _open_stage(live, _check())
    state = live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{SESSION}');
      const ref = root.querySelector('.gc-ref'); ref.value = '2700'; ref.dispatchEvent(new Event('input', {{bubbles: true}}));
      const axis = root.querySelector('.gc-axis'); axis.value = '+y'; axis.dispatchEvent(new Event('change', {{bubbles: true}}));
      const kept = ref.value;
      window.__stage.update({json.dumps(_check())});      // a re-draw keeps the typed length too
      return {{kept, redrawn: document.querySelector('#gstage-{SESSION} .gc-ref').value,
               axis: document.querySelector('#gstage-{SESSION} .gc-axis').value}};
    }})()""")
    assert state == {"kept": "2700", "redrawn": "2700", "axis": "+y"}, state
    body = _proceed(live)
    assert body["flow_axis"] == "+y" and body["reference_length_mm"] == 2700 and body["reference_length_typed"] is True


def test_a_reference_length_box_left_blank_is_the_length_along_the_flow(live):
    """Clearing a typed length hands the box back to the axis: it shows the part's length along
    the flow when the user leaves it, and a blank box is never confirmed as no length at all."""
    _open_stage(live, _check())
    state = live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{SESSION}');
      const ref = root.querySelector('.gc-ref');
      ref.value = '2700'; ref.dispatchEvent(new Event('input', {{bubbles: true}}));
      const axis = root.querySelector('.gc-axis'); axis.value = '+y'; axis.dispatchEvent(new Event('change', {{bubbles: true}}));
      ref.value = ''; ref.dispatchEvent(new Event('input', {{bubbles: true}}));
      const blank = ref.value;
      ref.dispatchEvent(new Event('change', {{bubbles: true}}));      // the user leaves the box
      return {{blank, left: ref.value}};
    }})()""")
    assert state == {"blank": "", "left": "1800"}, state
    body = _proceed(live)
    assert body["flow_axis"] == "+y" and body["reference_length_mm"] == 1800 and body["reference_length_typed"] is False


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


def test_the_reference_length_follows_the_axis_on_the_card_and_through_its_redraw(live):
    check = _check(); check["skin"] = False
    check["proposal"].update(flow_axis="+y", reference_length_mm=1800.0)
    live.evaluate("""(async () => {
      const CHECK = __CHECK__;
      window.__confirmed = null;
      const { Stage } = await import('/static/js/render/stage.js');
      window.__Stage = Stage;
      Stage.geometryCheck(CHECK, async (body) => { window.__confirmed = body; return {message: 'ok'}; });
    })()""".replace("__CHECK__", json.dumps(check)), timeout=60)
    live.wait_for("!!document.querySelector('.gc-card')", timeout=30, what="the card to render")
    state = live.evaluate("""(() => {
      const card = () => document.querySelector('.gc-card');
      const axis = card().querySelector('.gc-axis');
      axis.value = '-x'; axis.dispatchEvent(new Event('change', {bubbles: true}));
      const turned = card().querySelector('.gc-ref').value;
      // the model's labels arrive and the card is drawn again from a fresh proposal
      window.__Stage.geometryCheck(__CHECK__, async (body) => { window.__confirmed = body; return {message: 'ok'}; });
      return {turned, axis: card().querySelector('.gc-axis').value, ref: card().querySelector('.gc-ref').value};
    })()""".replace("__CHECK__", json.dumps(check)))
    assert state == {"turned": "4200", "axis": "-x", "ref": "4200"}, state
    live.evaluate("document.querySelector('.gc-card .gc-proceed').click()")
    live.wait_for("window.__confirmed !== null", timeout=30, what="the card's confirmation")
    body = live.evaluate("window.__confirmed")
    assert body["flow_axis"] == "-x" and body["reference_length_mm"] == 4200 and body["reference_length_typed"] is False
