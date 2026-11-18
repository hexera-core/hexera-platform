# Responsibility: Verify the flow-kind question asked beside the open stage (the part still undecided once named) shows its two buttons, and that a press sets the stage's "The fluid flows" and "The file is" before Proceed - and Proceed sends what was answered.
# Boundaries: the page the user is served, with its own composer and stage; only the network is stood in for (the check, the skin, the chat reply as the server gives it).
from __future__ import annotations

import json

import pytest
from conftest import assert_clean

pytestmark = pytest.mark.ui

SESSION = "flow-kind-ask"


def _skin() -> dict:
    from meshpipeline.cad.stl_io import _box_triangles
    from meshpipeline.render.viewer_pack import stl_response
    resp = stl_response({"skin": _box_triangles((0.0, 0.0, 0.0), (0.04, 0.04, 0.2))}, {}, "m")
    resp.update(is_mesh=False, cell_count=0)
    return resp


def _check() -> dict:
    from meshpipeline.agents.intake import flow
    from meshpipeline.application.purpose_map import choices_for
    record = {"kind": "undecided", "flow": "external", "best_guess": "external", "confidence": 0.5, "by": "guess",
              "rule": "FLOW-4", "reasons": ["the shape alone cannot tell"], "question": flow.FLOW_KIND_QUESTION,
              "input_kind": "solid-body"}
    return {"status": "ready", "named": True, "skin": True, "pictures": [], "route": "flow",
            "ask": {"reply": flow.FLOW_KIND_QUESTION, "choices": choices_for(flow.FLOW_KIND_QUESTION)},
            "proposal": {"part": "a bar", "input_kind": "solid-body", "flow": "external", "flow_kind": record,
                         "openings": [{"id": 1, "name": "end_1", "role": "inlet", "shape": "circle", "diameter_mm": 40.0,
                                       "centroid_mm": [20.0, 20.0, 0.0], "normal": [0, 0, -1], "confidence": 0.5},
                                      {"id": 2, "name": "end_2", "role": "outlet", "shape": "circle", "diameter_mm": 40.0,
                                       "centroid_mm": [20.0, 20.0, 200.0], "normal": [0, 0, 1], "confidence": 0.5}],
                         "size_mm": [40.0, 40.0, 200.0], "seed_point_mm": None, "notes": [], "flow_axis": "+z",
                         "reference_length_mm": 200.0, "extents": {"upstream": 5, "downstream": 10, "lateral": 5,
                                                                   "vertical": 5}, "grounded": False,
                         "unit": "mm", "unit_basis": "file_declared", "scale_to_m": 0.001}}


def test_a_press_on_the_flow_kind_question_sets_the_stage_before_proceed(live):
    from meshpipeline.application.geometry_hold import HOLD_REPLY
    chat_reply = {"session_id": SESSION, "reply": HOLD_REPLY, "choices": [], "done": False,
                  "stage_flow": {"flow": "internal", "input_kind": "fluid-domain"}}
    live.evaluate("""(async () => {
      const SKIN = __SKIN__, CHECK = __CHECK__, REPLY = __REPLY__;
      window.__sent = []; window.__confirmed = null;
      const json = (v) => Promise.resolve(new Response(JSON.stringify(v), {status: 200, headers: {'Content-Type': 'application/json'}}));
      const real = window.fetch.bind(window);
      let polls = 0;
      window.fetch = (u, o) => {
        const url = String(u);
        if (url.includes('/geometry/__S__/check/skin')) return json(SKIN);
        if (url.includes('/geometry/__S__/check/confirm')) { window.__confirmed = JSON.parse(o.body); return json({message: 'ok', next: null}); }
        if (url.includes('/geometry/__S__/check')) { polls += 1; const c = {...CHECK}; if (polls > 1) delete c.ask; return json(c); }
        if (url.includes('/chat/message')) { window.__sent.push(JSON.parse(o.body).content); return json(REPLY); }
        return real(u, o);
      };
      const { set } = await import('/static/js/core/state.js');
      set.sessionId('__S__');
      const C = await import('/static/js/shell/composer.js');
      C.watchGeometryCheck('__S__');
    })()""".replace("__SKIN__", json.dumps(_skin())).replace("__CHECK__", json.dumps(_check()))
       .replace("__REPLY__", json.dumps(chat_reply)).replace("__S__", SESSION), timeout=60)
    live.wait_for(f"window._vdbg && window._vdbg['gstage:{SESSION}']", timeout=90, what="the geometry stage to open")
    live.wait_for("[...document.querySelectorAll('.qc-btn')].some(b => b.textContent === 'Through the inside')",
                  timeout=30, what="the flow-kind question's buttons")
    before = live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{SESSION}');
      return {{flow: root.querySelector('.gc-flow').value, kind: root.querySelector('.gc-kind').value,
               buttons: [...document.querySelectorAll('.qc-btn')].map(b => b.textContent)}};
    }})()""")
    assert before == {"flow": "external", "kind": "solid-body",
                      "buttons": ["Through the inside", "Around the outside"]}, before

    live.evaluate("""(() => { [...document.querySelectorAll('.qc-btn')].find(b => b.textContent === 'Through the inside').click(); })()""")
    live.wait_for(f"document.getElementById('gstage-{SESSION}').querySelector('.gc-flow').value === 'internal'",
                  timeout=30, what="the stage's flow to take the answer")
    after = live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{SESSION}');
      return {{sent: window.__sent, kind: root.querySelector('.gc-kind').value,
               intHidden: root.querySelector('.gc-int').hidden, extHidden: root.querySelector('.gc-ext').hidden}};
    }})()""")
    assert after["sent"] == ["The flow goes through the inside"], after
    assert after["kind"] == "fluid-domain" and after["intHidden"] is False and after["extHidden"] is True, after

    # Proceed confirms what was answered
    live.evaluate(f"""(() => {{ document.querySelector('#gstage-{SESSION} .gc-proceed').click(); }})()""")
    live.wait_for("window.__confirmed !== null", timeout=30, what="the confirmation to be sent")
    body = live.evaluate("window.__confirmed")
    assert body["flow"] == "internal" and body["input_kind"] == "fluid-domain", body
    assert_clean(live, "the flow-kind question beside the stage")
