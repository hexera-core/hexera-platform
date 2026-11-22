# Responsibility: Verify INS-23's question on the stage: a part read as a perforated plate shows "This looks like a
# perforated plate: ... Flow goes through the holes?" with its three answers; "No, each hole is its own passage"
# swaps the openings for the holes (rows and pins), "Yes" brings the plate's two sides back, "No, the flow goes
# around it" turns the flow to around the part - and Proceed sends what was answered.
# Boundaries: the page the user is served, with its own composer and stage; only the network is stood in for (the
# check, the skin).
from __future__ import annotations

import json

import pytest
from conftest import assert_clean

pytestmark = pytest.mark.ui

SESSION = "perforated-ask"
QUESTION = "This looks like a perforated plate: 4 holes of 4 mm through a 5 mm plate. Flow goes through the holes?"
ANSWERS = ["Yes", "No, each hole is its own passage", "No, the flow goes around it"]


def _skin() -> dict:
    from meshpipeline.cad.stl_io import _box_triangles
    from meshpipeline.render.viewer_pack import stl_response
    resp = stl_response({"skin": _box_triangles((-0.03, -0.03, 0.0), (0.03, 0.03, 0.005))}, {}, "m")
    resp.update(is_mesh=False, cell_count=0)
    return resp


def _opening(i: int, name: str, role: str, d: float, z: float, x: float = 0.0) -> dict:
    return {"id": i, "name": name, "role": role, "shape": "circle", "kind": "ring", "diameter_mm": d,
            "centroid_mm": [x, 0.0, z], "normal": [0, 0, 1 if z >= 0 else -1], "confidence": 0.7}


def _check() -> dict:
    sides = [_opening(1, "inlet", "inlet", 67.7, 29.0), _opening(2, "outlet", "outlet", 67.7, -24.0)]
    holes = [_opening(k + 1, "inlet" if k == 0 else f"outlet_{k}", "inlet" if k == 0 else "outlet", 4.0, 5.0,
                      -12.0 + 8.0 * k) for k in range(4)]
    return {"status": "ready", "named": True, "skin": True, "pictures": [], "route": "flow",
            "proposal": {"part": "a sieve", "input_kind": "body-surface", "flow": "internal",
                         "openings": sides, "size_mm": [60.0, 60.0, 5.0], "seed_point_mm": None, "notes": [],
                         "perforated": {"rule": "INS-23", "kind": "plate", "question": QUESTION, "answers": ANSWERS,
                                        "holes_openings": holes},
                         "unit": "mm", "unit_basis": "file_declared", "scale_to_m": 0.001}}


def test_the_perforated_question_swaps_the_openings_and_proceed_sends_the_answer(live):
    live.evaluate("""(async () => {
      const SKIN = __SKIN__, CHECK = __CHECK__;
      window.__confirmed = null;
      const json = (v) => Promise.resolve(new Response(JSON.stringify(v), {status: 200, headers: {'Content-Type': 'application/json'}}));
      const real = window.fetch.bind(window);
      window.fetch = (u, o) => {
        const url = String(u);
        if (url.includes('/geometry/__S__/check/skin')) return json(SKIN);
        if (url.includes('/geometry/__S__/check/confirm')) { window.__confirmed = JSON.parse(o.body); return json({message: 'ok', next: null}); }
        if (url.includes('/geometry/__S__/check')) return json(CHECK);
        return real(u, o);
      };
      const { set } = await import('/static/js/core/state.js');
      set.sessionId('__S__');
      const C = await import('/static/js/shell/composer.js');
      C.watchGeometryCheck('__S__');
    })()""".replace("__SKIN__", json.dumps(_skin())).replace("__CHECK__", json.dumps(_check()))
       .replace("__S__", SESSION), timeout=60)
    live.wait_for(f"window._vdbg && window._vdbg['gstage:{SESSION}']", timeout=90, what="the geometry stage to open")
    read = f"""(() => {{
      const root = document.getElementById('gstage-{SESSION}');
      return {{question: (root.querySelector('.gc-perf .gc-note') || {{}}).textContent,
               answers: [...root.querySelectorAll('.gc-perf label')].map(l => l.textContent.trim()),
               rows: root.querySelectorAll('.gc-table tbody tr').length,
               pins: window._vdbg['gstage:{SESSION}'].pins(), flow: root.querySelector('.gc-flow').value}};
    }})()"""
    live.wait_for(f"document.querySelector('#gstage-{SESSION} .gc-perf')", timeout=30, what="the perforated question")
    assert live.evaluate(read) == {"question": QUESTION, "answers": ANSWERS, "rows": 2, "pins": 2, "flow": "internal"}

    pick = "(() => {{ document.querySelector('#gstage-{s} .gc-perf-a[value=\"{k}\"]').click(); }})()"
    live.evaluate(pick.format(s=SESSION, k=1))
    live.wait_for(f"document.querySelectorAll('#gstage-{SESSION} .gc-table tbody tr').length === 4", timeout=30,
                  what="the holes as the openings")
    assert live.evaluate(read)["pins"] == 4
    live.evaluate(pick.format(s=SESSION, k=0))
    live.wait_for(f"document.querySelectorAll('#gstage-{SESSION} .gc-table tbody tr').length === 2", timeout=30,
                  what="the plate's two sides back")
    live.evaluate(pick.format(s=SESSION, k=2))
    live.wait_for(f"document.querySelector('#gstage-{SESSION} .gc-flow').value === 'external'", timeout=30,
                  what="the flow around the part")
    live.evaluate(pick.format(s=SESSION, k=0))
    live.wait_for(f"document.querySelector('#gstage-{SESSION} .gc-flow').value === 'internal'", timeout=30,
                  what="the flow through the part again")

    live.evaluate(f"""(() => {{ document.querySelector('#gstage-{SESSION} .gc-proceed').click(); }})()""")
    live.wait_for("window.__confirmed !== null", timeout=30, what="the confirmation to be sent")
    body = live.evaluate("window.__confirmed")
    assert body["flow"] == "internal"
    assert sorted(o["name"] for o in body["openings"]) == ["inlet", "outlet"], body["openings"]
    assert_clean(live, "the perforated question on the stage")
