# Responsibility: Verify what the user changes on the geometry form is kept through a re-draw and
# said back in one plain line when they proceed - on the stage and on the card alike.
# Boundaries: the skin payload is the one the worker stores; only the network is stood in for.
from __future__ import annotations

import json

import pytest
from test_geometry_stage_external import SESSION, _check, _open_stage, _skin

pytestmark = pytest.mark.ui


def _lines(live, cases: list[tuple[dict, dict]]) -> list[str]:
    return live.evaluate("""(async () => {
      const { editsLine } = await import('/static/js/render/geometry_form.js');
      return __CASES__.map(([served, body]) => editsLine(served, body, served));
    })()""".replace("__CASES__", json.dumps(cases)))


def test_the_line_says_each_change_against_what_the_check_proposed(live):
    car = _check()["proposal"]
    duct = {"input_kind": "body-surface", "flow": "internal", "unit": "mm", "openings": [
        {"id": 1, "name": "inlet", "role": "inlet"}, {"id": 2, "name": "outlet", "role": "outlet"},
        {"id": 3, "name": "", "role": "not_an_opening"}]}
    same_car = {"input_kind": "solid-body", "flow": "external", "unit": "mm", "flow_axis": "+x",
                "reference_length_mm": 4200.6, "reference_length_typed": False,
                "extents": {"upstream": 5, "downstream": 10, "lateral": 5, "vertical": 5}, "grounded": True}
    turned_car = {**same_car, "flow_axis": "+y", "unit": "m", "grounded": False,
                  "extents": {"upstream": 3, "downstream": 10, "lateral": 5, "vertical": 5}}
    edited_duct = {"input_kind": "fluid-domain", "flow": "internal", "unit": "mm", "openings": [
        {"id": 1, "name": "water_in", "role": "inlet"}, {"id": 3, "name": "opening_3", "role": "outlet"},
        {"id": 7, "name": "drain", "role": "outlet"}]}
    out = _lines(live, [(car, same_car), (car, turned_car), (duct, edited_duct)])

    assert out[0] == "", f"nothing was changed, and the line said: {out[0]}"
    assert out[1] == ("Noted your changes on the picture: the file is in metres (was millimetres); "
                      "the fluid travels along +y (was +x); far field, in part lengths: 3 upstream (was 5); "
                      "the part is not on the ground."), out[1]
    assert out[2] == ("Noted your changes on the picture: the file is the fluid volume (was a hollow wall); "
                      "opening 1 named water_in; opening 3 is an outlet (was not an opening); "
                      "opening 7 added as drain (an outlet); opening 2 removed."), out[2]


def test_the_stage_keeps_the_users_answers_when_the_models_labels_land(live):
    # The naming lands while the user edits, and the form is drawn again from its proposal: the far
    # field and the ground went back to the check's values, and Proceed confirmed those.
    _open_stage(live, _check())
    live.evaluate("""(() => {
      const root = document.getElementById('gstage-__S__');
      const up = root.querySelector('.gc-ext-upstream'); up.value = '3'; up.dispatchEvent(new Event('input', {bubbles: true}));
      const g = root.querySelector('.gc-ground'); g.checked = false; g.dispatchEvent(new Event('change', {bubbles: true}));
      window.__stage.update(__CHECK__);                     // the model's labels land
    })()""".replace("__S__", SESSION).replace("__CHECK__", json.dumps(_check())))
    kept = live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{SESSION}');
      return {{up: root.querySelector('.gc-ext-upstream').value, ground: root.querySelector('.gc-ground').checked,
               down: root.querySelector('.gc-ext-downstream').value}};
    }})()""")
    assert kept == {"up": "3", "ground": False, "down": "10"}, kept

    # proceeding sends the user's answers, not the check's
    live.evaluate(f"document.querySelector('#gstage-{SESSION} .gc-proceed').click()")
    live.wait_for("window.__confirmed !== null", timeout=30, what="the confirmation to be sent")
    body = live.evaluate("window.__confirmed")
    assert body["extents"]["upstream"] == 3 and body["grounded"] is False, body


def test_the_stage_hands_the_edits_line_to_the_confirmation(live):
    check = _check()
    live.evaluate("""(async () => {
      const SKIN = __SKIN__;
      const real = window.fetch.bind(window);
      window.fetch = (u, o) => String(u).includes('/geometry/__S__-said/check/skin')
        ? Promise.resolve(new Response(JSON.stringify(SKIN), {status: 200, headers: {'Content-Type': 'application/json'}}))
        : real(u, o);
      window.__said = null;
      const { openGeometryStage } = await import('/static/js/viewer/geometry_stage.js');
      await openGeometryStage('__S__-said', __CHECK__,
        async (body, edits) => { window.__said = {edits: edits || ''}; return {message: 'ok'}; },
        {anchorEl: document.getElementById('stage')});
    })()""".replace("__SKIN__", json.dumps(_skin()))
       .replace("__CHECK__", json.dumps(check)).replace("__S__", SESSION), timeout=60)
    live.wait_for(f"window._vdbg && window._vdbg['gstage:{SESSION}-said']", timeout=90,
                  what="the geometry stage to initialise")
    live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{SESSION}-said');
      const axis = root.querySelector('.gc-axis'); axis.value = '+y'; axis.dispatchEvent(new Event('change', {{bubbles: true}}));
      root.querySelector('.gc-proceed').click();
    }})()""")
    live.wait_for("window.__said !== null", timeout=30, what="the confirmation to be sent")
    said = live.evaluate("window.__said.edits")
    assert said.startswith("Noted your changes on the picture: the fluid travels along +y (was +x)"), said


def test_the_card_keeps_only_what_the_user_changed_when_it_is_drawn_again(live):
    # The card put back the measuring step's kind over the model's, because it carried every
    # select across the re-draw whether the user had touched it or not.
    check = _check(); check["skin"] = False
    named = json.loads(json.dumps(check)); named["proposal"]["input_kind"] = "fluid-domain"
    state = live.evaluate("""(async () => {
      window.__said = null;
      const { Stage } = await import('/static/js/render/stage.js');
      const confirm = async (body, edits) => { window.__said = {body, edits: edits || ''}; return {message: 'ok'}; };
      Stage.geometryCheck(__CHECK__, confirm);
      const card = () => document.querySelector('.gc-card');
      const lat = card().querySelector('.gc-ext-lateral'); lat.value = '8'; lat.dispatchEvent(new Event('input', {bubbles: true}));
      Stage.geometryCheck(__NAMED__, confirm);              // the model's labels land: a new kind
      const kept = {kind: card().querySelector('.gc-kind').value, lat: card().querySelector('.gc-ext-lateral').value};
      card().querySelector('.gc-proceed').click();
      await new Promise(r => setTimeout(r, 200));
      return {kept, said: window.__said};
    })()""".replace("__CHECK__", json.dumps(check)).replace("__NAMED__", json.dumps(named)))
    assert state["kept"] == {"kind": "fluid-domain", "lat": "8"}, state["kept"]
    assert state["said"]["body"]["extents"]["lateral"] == 8
    assert state["said"]["edits"] == ("Noted your changes on the picture: "
                                      "far field, in part lengths: 8 to each side (was 5)."), state["said"]
