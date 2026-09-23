# Responsibility: Verify the stage opens the moment the part is measured - labels greyed out under
# the naming banner, nothing editable - fills in the model's labels when they arrive, lets the
# user add a sticker (snapped to a measured face, or free with a size to type) and remove one,
# and sends exactly those openings when proceeding. Also that the CAD-style skin draws: smooth
# normals, sharp edges, the view cube.
# Boundaries: the skin is the one the worker stores, built through the same code; only the
# network is stood in for. Pixel picking is not driven here (the hook adds at a point instead).
from __future__ import annotations

import json

import pytest
from conftest import assert_clean

pytestmark = pytest.mark.ui

SESSION = "stage-naming"


def _skin() -> dict:
    import numpy as np

    from meshpipeline.cad.stl_io import _box_triangles
    from meshpipeline.render.skin_mesh import edges_block, prepare_skin, skin_patch
    prep = prepare_skin(np.asarray(_box_triangles((0.0, 0.0, 0.0), (1.0, 0.5, 0.5)), dtype=float))
    return {"kind": "skin", "mesh_units": "m", "is_mesh": False, "cell_count": 0,
            "patches": [skin_patch(prep)], "edges": edges_block(prep)}


def _openings(named: bool) -> list:
    return [
        {"id": 1, "name": "water_in" if named else "inlet", "role": "inlet", "shape": "circle", "diameter_mm": 400.0,
         "centroid_m": [0.0, 0.25, 0.25], "centroid_mm": [0.0, 250.0, 250.0], "normal": [-1, 0, 0], "confidence": 0.9 if named else 0.6},
        {"id": 2, "name": "air_out" if named else "outlet", "role": "outlet", "shape": "circle", "diameter_mm": 400.0,
         "centroid_m": [1.0, 0.25, 0.25], "centroid_mm": [1000.0, 250.0, 250.0], "normal": [1, 0, 0], "confidence": 0.8 if named else 0.6},
    ]


def _faces() -> list:
    # the two mouths, and a flat face on the top the scout measured but did not propose
    return [{"face": 1, "kind": "ring", "shape": "circle", "centroid_m": [0.0, 0.25, 0.25], "centroid_mm": [0.0, 250.0, 250.0],
             "normal": [-1, 0, 0], "area_mm2": 125664.0, "diameter_mm": 400.0},
            {"face": 2, "kind": "ring", "shape": "circle", "centroid_m": [1.0, 0.25, 0.25], "centroid_mm": [1000.0, 250.0, 250.0],
             "normal": [1, 0, 0], "area_mm2": 125664.0, "diameter_mm": 400.0},
            {"face": 7, "kind": "disc", "shape": "circle", "centroid_m": [0.5, 0.25, 0.5], "centroid_mm": [500.0, 250.0, 500.0],
             "normal": [0, 0, 1], "area_mm2": 7854.0, "diameter_mm": 100.0}]


def _check(status: str) -> dict:
    named = status == "ready"
    return {"status": status, "named": named, "skin": True, "pictures": [], "proposal": {
        "part": "test duct" if named else "", "input_kind": "body-surface", "flow": "internal",
        "size_mm": [1000.0, 500.0, 500.0], "seed_point_mm": [500.0, 250.0, 250.0], "notes": [],
        "openings": _openings(named), "faces": _faces()}}


def test_the_stage_opens_greyed_out_while_naming_then_takes_the_models_labels_and_the_users_stickers(live):
    live.evaluate("""(async () => {
      const SKIN = __SKIN__, SCOUTED = __SCOUTED__;
      const real = window.fetch.bind(window);
      window.fetch = (u, o) => String(u).includes('/geometry/__S__/check/skin')
        ? Promise.resolve(new Response(JSON.stringify(SKIN), {status: 200, headers: {'Content-Type': 'application/json'}}))
        : real(u, o);
      window.__confirmed = null; window.__fellBack = false;
      const { openGeometryStage } = await import('/static/js/viewer/geometry_stage.js');
      window.__stage = await openGeometryStage('__S__', SCOUTED,
        async (body) => { window.__confirmed = body; return {message: 'ok'}; },
        {anchorEl: document.getElementById('stage'), fallback: () => { window.__fellBack = true; }});
    })()""".replace("__SKIN__", json.dumps(_skin())).replace("__SCOUTED__", json.dumps(_check("scouted")))
       .replace("__S__", SESSION), timeout=60)
    live.wait_for(f"window._vdbg && window._vdbg['gstage:{SESSION}']", timeout=90,
                  what="the geometry stage to initialise")

    # SCOUTED: the part is there to turn, the code's labels are shown greyed out, nothing is
    # editable, Proceed and Add are off, the banner says the naming is under way
    naming = live.evaluate(f"""(() => {{
      const h = window._vdbg['gstage:{SESSION}'], root = document.getElementById('gstage-{SESSION}');
      return {{fellBack: window.__fellBack, naming: window.__stage.isNaming(), pins: h.pins(),
               edges: h.edges(), smooth: h.smooth(), cube: h.cube(),
               banner: root.querySelector('.gc-banner').textContent.trim(), bannerHidden: root.querySelector('.gc-banner').hidden,
               greyed: root.querySelector('.gc-form').classList.contains('gc-naming'),
               allOff: [...root.querySelectorAll('.gc-form input, .gc-form select, .gc-form button')].every(e => e.disabled),
               names: [...root.querySelectorAll('.gc-name')].map(i => i.value),
               views: root.querySelectorAll('.gc-view').length}};
    }})()""")
    assert naming["fellBack"] is False and naming["naming"] is True and naming["pins"] == 2, naming
    assert naming["edges"] == 12 and naming["smooth"] is True and naming["cube"] is True, naming
    assert naming["bannerHidden"] is False and "Naming" in naming["banner"], naming
    assert naming["greyed"] is True and naming["allOff"] is True and naming["names"] == ["inlet", "outlet"], naming
    assert naming["views"] == 4

    # READY: the model's labels replace the code's, the form opens, the banner goes; the stickers
    # keep the code's positions
    ready = live.evaluate(f"""(() => {{
      window.__stage.update(__READY__);
      const h = window._vdbg['gstage:{SESSION}'], root = document.getElementById('gstage-{SESSION}');
      return {{naming: window.__stage.isNaming(), pins: h.pins(), bannerHidden: root.querySelector('.gc-banner').hidden,
               greyed: root.querySelector('.gc-form').classList.contains('gc-naming'),
               allOn: [...root.querySelectorAll('.gc-form input, .gc-form select, .gc-form button')].every(e => !e.disabled),
               names: [...root.querySelectorAll('.gc-name')].map(i => i.value),
               lead: root.querySelector('.gc-lead b').textContent}};
    }})()""".replace("__READY__", json.dumps(_check("ready"))))
    assert ready["naming"] is False and ready["pins"] == 2 and ready["bannerHidden"] is True, ready
    assert ready["greyed"] is False and ready["allOn"] is True, ready
    assert ready["names"] == ["water_in", "air_out"] and ready["lead"] == "test duct", ready

    # ADD: a point near the measured top face snaps to it and takes its size; a point off any
    # measured face lands where it is, with a box for the size. DELETE: the row's button takes
    # the row and its pin.
    edited = live.evaluate(f"""(() => {{
      const h = window._vdbg['gstage:{SESSION}'], root = document.getElementById('gstage-{SESSION}');
      const snapped = h.add([0.51, 0.26, 0.5], [0, 0, 1]);
      const free = h.add([0.2, 0.0, 0.1], [0, -1, 0]);
      const freeRow = root.querySelector('tr[data-id="' + free.id + '"]');
      freeRow.querySelector('.gc-dia').value = '55';
      freeRow.querySelector('.gc-name').value = 'drain';
      root.querySelector('tr[data-id="2"] .gc-x').click();
      return {{snapped: {{id: snapped.id, on: !!snapped.snapped, d: snapped.diameter_mm, at: snapped.centroid_mm}},
               free: {{id: free.id, on: !!free.snapped, shape: free.shape, box: !!freeRow.querySelector('.gc-dia')}},
               ids: h.openings(), pins: h.pins(), selected: h.selected(),
               rows: [...root.querySelectorAll('.gc-table tbody tr')].map(tr => tr.dataset.id),
               pinEls: [...root.querySelectorAll('.gc-pin')].map(p => p.textContent)}};
    }})()""")
    assert edited["snapped"] == {"id": 3, "on": True, "d": 100.0, "at": [500.0, 250.0, 500.0]}, edited
    assert edited["free"] == {"id": 4, "on": False, "shape": "unknown", "box": True}, edited
    assert edited["ids"] == [1, 3, 4] and edited["pins"] == 3 and edited["rows"] == ["1", "3", "4"], edited
    assert sorted(edited["pinEls"]) == ["1", "3", "4"] and edited["selected"] == 4, edited

    # PROCEED sends what is on the table now: the model's name for 1, the snapped face's size
    # for 3, the typed size for 4, and nothing of 2
    live.evaluate(f"""(() => {{ document.querySelector('#gstage-{SESSION} .gc-proceed').click(); }})()""")
    live.wait_for("window.__confirmed !== null", timeout=30, what="the confirmation to be sent")
    body = live.evaluate("window.__confirmed")
    assert [o["id"] for o in body["openings"]] == [1, 3, 4]
    assert [o["name"] for o in body["openings"]] == ["water_in", "opening_3", "drain"]
    assert body["openings"][1]["diameter_mm"] == 100.0 and body["openings"][1]["centroid_mm"] == [500.0, 250.0, 500.0]
    assert body["openings"][2]["diameter_mm"] == 55 and body["openings"][2]["centroid_mm"] == [200.0, 0.0, 100.0]
    assert body["part"] == "test duct" and body["flow"] == "internal"
    assert live.evaluate(f"!document.getElementById('gstage-{SESSION}')") is True
    assert_clean(live, "the naming-mode geometry stage")


def test_when_the_naming_gives_up_the_codes_labels_open_for_editing(live):
    live.evaluate("""(async () => {
      const SKIN = __SKIN__, SCOUTED = __SCOUTED__;
      const real = window.fetch.bind(window);
      window.fetch = (u, o) => String(u).includes('/geometry/__S__-gaveup/check/skin')
        ? Promise.resolve(new Response(JSON.stringify(SKIN), {status: 200, headers: {'Content-Type': 'application/json'}}))
        : real(u, o);
      const { openGeometryStage } = await import('/static/js/viewer/geometry_stage.js');
      window.__stage2 = await openGeometryStage('__S__-gaveup', SCOUTED, async () => ({message: 'ok'}),
        {anchorEl: document.getElementById('stage'), fallback: () => {}});
    })()""".replace("__SKIN__", json.dumps(_skin())).replace("__SCOUTED__", json.dumps(_check("scouted")))
       .replace("__S__", SESSION), timeout=60)
    live.wait_for(f"window._vdbg && window._vdbg['gstage:{SESSION}-gaveup']", timeout=90,
                  what="the geometry stage to initialise")
    state = live.evaluate(f"""(() => {{
      window.__stage2.update({{status: 'failed', named: false, reason: 'the model timed out'}});
      const root = document.getElementById('gstage-{SESSION}-gaveup');
      return {{naming: window.__stage2.isNaming(), banner: root.querySelector('.gc-banner').textContent.trim(),
               warn: root.querySelector('.gc-banner').classList.contains('warn'),
               allOn: [...root.querySelectorAll('.gc-form input, .gc-form select, .gc-form button')].every(e => !e.disabled),
               names: [...root.querySelectorAll('.gc-name')].map(i => i.value)}};
    }})()""")
    assert state["naming"] is False and state["allOn"] is True and state["names"] == ["inlet", "outlet"], state
    assert state["warn"] is True and "gave up" in state["banner"], state
    live.evaluate("window.__stage2.release()")
    assert_clean(live, "the geometry stage after the naming gave up")
