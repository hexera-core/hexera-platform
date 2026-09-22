# Responsibility: Verify the geometry check opens as a 3D stage in a real browser - the part with a
# pin on every opening, a pin click selecting its row, and proceeding confirming the edited form
# and handing the workbench back.
# Boundaries: the skin payload is the one the worker stores; only the network is stood in for.
from __future__ import annotations

import json

import pytest
from conftest import assert_clean

pytestmark = pytest.mark.ui

SESSION = "stage-session"


def _skin() -> dict:
    from meshpipeline.cad.stl_io import _box_triangles
    from meshpipeline.render.viewer_pack import stl_response
    resp = stl_response({"skin": _box_triangles((0.0, 0.0, 0.0), (1.0, 0.5, 0.5))}, {}, "m")
    resp.update(is_mesh=False, cell_count=0)
    return resp


def _check() -> dict:
    # two openings on the box's short ends, as the scout would report them: metres for the
    # scene, millimetres for the person
    return {"status": "ready", "skin": True, "pictures": [], "proposal": {
        "part": "test duct", "input_kind": "body-surface", "flow": "internal",
        "size_mm": [1000.0, 500.0, 500.0], "seed_point_mm": [500.0, 250.0, 250.0], "notes": [],
        "openings": [
            {"id": 1, "name": "inlet", "role": "inlet", "shape": "circle", "diameter_mm": 400.0,
             "centroid_m": [0.0, 0.25, 0.25], "centroid_mm": [0.0, 250.0, 250.0], "normal": [-1, 0, 0], "confidence": 0.9},
            {"id": 2, "name": "outlet", "role": "outlet", "shape": "circle", "diameter_mm": 400.0,
             "centroid_m": [1.0, 0.25, 0.25], "centroid_mm": [1000.0, 250.0, 250.0], "normal": [1, 0, 0], "confidence": 0.8},
        ]}}


def test_the_check_opens_as_a_stage_and_proceeding_confirms_the_edited_form(live):
    live.evaluate("""(async () => {
      const SKIN = __SKIN__, CHECK = __CHECK__;
      const real = window.fetch.bind(window);
      window.fetch = (u, o) => {
        const s = String(u);
        if (s.includes('/geometry/__S__/check/skin'))
          return Promise.resolve(new Response(JSON.stringify(SKIN),
            {status: 200, headers: {'Content-Type': 'application/json'}}));
        return real(u, o);
      };
      window.__confirmed = null;
      const { openGeometryStage } = await import('/static/js/viewer/geometry_stage.js');
      await openGeometryStage('__S__', CHECK,
        async (body) => { window.__confirmed = body; return {message: 'ok'}; },
        {anchorEl: document.getElementById('stage'), fallback: () => { window.__fellBack = true; }});
    })()""".replace("__SKIN__", json.dumps(_skin())).replace("__CHECK__", json.dumps(_check()))
       .replace("__S__", SESSION), timeout=60)
    live.wait_for(f"window._vdbg && window._vdbg['gstage:{SESSION}']", timeout=90,
                  what="the geometry stage to initialise")

    # the stage took the workbench, with one pin per opening on the canvas
    opened = live.evaluate(f"""(() => {{
      const h = window._vdbg['gstage:{SESSION}'];
      return {{pins: h.pins(), selected: h.selected(), fellBack: !!window.__fellBack,
               wb: document.getElementById('app').classList.contains('wb'),
               inWorkbench: !!document.querySelector('#workbench #gstage-{SESSION}'),
               rows: document.querySelectorAll('#gstage-{SESSION} .gc-table tbody tr').length,
               pinEls: document.querySelectorAll('#gstage-{SESSION} .gc-pin').length}};
    }})()""")
    assert opened["fellBack"] is False, opened
    assert opened["pins"] == 2 and opened["pinEls"] == 2 and opened["rows"] == 2, opened
    assert opened["wb"] is True and opened["inWorkbench"] is True, opened
    assert opened["selected"] is None

    # clicking pin 2 selects its row; looking into opening 1 selects that one instead
    picked = live.evaluate(f"""(() => {{
      const h = window._vdbg['gstage:{SESSION}'];
      [...document.querySelectorAll('#gstage-{SESSION} .gc-pin')].find(p => p.textContent === '2').click();
      const afterPin = {{sel: h.selected(),
        row: document.querySelector('#gstage-{SESSION} .gc-table tr.sel')?.dataset.id || null}};
      h.look(1);
      return {{afterPin, afterLook: h.selected(), visible: h.visible()}};
    }})()""")
    assert picked["afterPin"] == {"sel": 2, "row": "2"}, picked
    assert picked["afterLook"] == 1, picked
    assert picked["visible"] >= 1

    # rename opening 1 and proceed: the confirm body carries the edit, and the page is handed back
    live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{SESSION}');
      const inp = root.querySelector('tr[data-id="1"] .gc-name'); inp.value = 'water_in';
      root.querySelector('tr[data-id="2"] .gc-role').value = 'outlet';
      root.querySelector('.gc-proceed').click();
    }})()""")
    live.wait_for("window.__confirmed !== null", timeout=30, what="the confirmation to be sent")
    done = live.evaluate(f"""(() => ({{
      body: window.__confirmed,
      gone: !document.getElementById('gstage-{SESSION}'),
      wb: document.getElementById('app').classList.contains('wb'),
      hook: !!(window._vdbg && window._vdbg['gstage:{SESSION}'])}}))()""")
    body = done["body"]
    assert body["input_kind"] == "body-surface" and body["flow"] == "internal"
    assert [o["name"] for o in body["openings"]] == ["water_in", "outlet"]
    assert body["openings"][0]["diameter_mm"] == 400.0 and body["openings"][0]["centroid_mm"] == [0.0, 250.0, 250.0]
    assert body["seed_point_mm"] == [500.0, 250.0, 250.0]
    assert done["gone"] is True and done["wb"] is False and done["hook"] is False, done
    assert_clean(live, "the geometry stage")


def test_a_check_without_a_skin_falls_back_to_the_card(live):
    live.evaluate("""(async () => {
      const CHECK = __CHECK__;
      const real = window.fetch.bind(window);
      window.fetch = (u, o) => String(u).includes('/geometry/__S__-bare/check/skin')
        ? Promise.resolve(new Response(JSON.stringify({detail: 'No skin'}),
            {status: 404, headers: {'Content-Type': 'application/json'}}))
        : real(u, o);
      window.__fellBack2 = false;
      const { openGeometryStage } = await import('/static/js/viewer/geometry_stage.js');
      const r = await openGeometryStage('__S__-bare', CHECK, async () => ({}),
        {anchorEl: document.getElementById('stage'), fallback: () => { window.__fellBack2 = true; }});
      window.__stageResult = r;
    })()""".replace("__CHECK__", json.dumps(_check())).replace("__S__", SESSION), timeout=60)
    live.wait_for("window.__fellBack2 === true", timeout=60, what="the stage to fall back to the card")
    state = live.evaluate(f"""(() => ({{
      result: window.__stageResult,
      gone: !document.getElementById('gstage-{SESSION}-bare'),
      wb: document.getElementById('app').classList.contains('wb')}}))()""")
    assert state["result"] is None and state["gone"] is True and state["wb"] is False, state
