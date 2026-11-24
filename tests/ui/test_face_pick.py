# Responsibility: Verify face picking on the settled intake model in a real browser (NOTE-17): no pick while the stage
# panel is open; after Proceed a plain click asks the server for the face there, lights it and puts one chip
# "Face 12 · round Ø15 mm ✕" under the box used last; a second click on it or its ✕ drops it; a drag is not a click; a
# click on no face does nothing; at most 20 faces; the send's chip clear takes the light with it.
# Boundaries: the skin is built through the worker's own code from a tube made here; the faces/at answer is stood in
# for in the page (no network, no model).
from __future__ import annotations

import base64
import json
import math

import numpy as np
import pytest
from conftest import assert_clean
from test_fea_stage_mesh_as import _check, _proceed
from test_geometry_stage_open_ends import LENGTH, ORIGIN, R_IN, R_OUT, _skin, _tube

pytestmark = pytest.mark.ui

S = 1 / math.sqrt(2)
# a point on the tube's outside that faces the opening view's camera (it looks from -x, -y, +z)
SPOT = [ORIGIN[0] + LENGTH / 2, ORIGIN[1] - R_OUT * S, ORIGIN[2] + R_OUT * S]


def _face() -> dict:
    """The server's answer for the face there: a small patch of triangles round the spot (metres)."""
    x, y, z, d = SPOT[0], SPOT[1], SPOT[2], 0.01
    pts = [(x - d, y, z), (x + d, y, z), (x + d, y - d * S, z + d * S), (x - d, y - d * S, z + d * S)]
    b64 = lambda a, t: base64.b64encode(np.asarray(a, dtype=t).ravel().tobytes()).decode("ascii")  # noqa: E731
    return {"id": "Face_12", "label": "Face 12 · round Ø15 mm",
            "sig": {"area_m2": 0.002, "centroid_m": SPOT, "normal": [0, -S, S]},
            "highlight": {"points_b64": b64(pts, np.float32), "tris_b64": b64([(0, 1, 2), (0, 2, 3)], np.uint32), "units": "m"}}


def _open(live, sid: str) -> None:
    live.evaluate("""(async () => {
      const SKIN = __SKIN__, CHECK = __CHECK__, FACE = __FACE__;
      const real = window.fetch.bind(window);
      const json = (x) => Promise.resolve(new Response(JSON.stringify(x), {status: 200, headers: {'Content-Type': 'application/json'}}));
      window.__asked = []; window.__noFace = false; window.__seq = 0;
      window.fetch = (u, o) => {
        const s = String(u);
        if (s.includes('/geometry/__S__/check/skin')) return json(SKIN);
        if (s.includes('/geometry/__S__/check/faces/at')) {
          window.__asked.push(JSON.parse(o.body));
          if (window.__noFace) return json({id: null});
          if (window.__seq) { window.__seq++; return json({...FACE, id: 'Face_' + window.__seq, label: 'Face ' + window.__seq}); }
          return json(FACE);
        }
        return real(u, o);
      };
      window.__confirmed = window.__confirmed || {};
      window.__chips = await import('/static/js/shell/chips.js');
      const { openGeometryStage } = await import('/static/js/viewer/geometry_stage.js');
      window.__stages = window.__stages || {};
      window.__stages['__S__'] = await openGeometryStage('__S__', CHECK,
        async (body) => { window.__confirmed['__S__'] = body; return {message: 'ok'}; },
        {anchorEl: document.getElementById('stage'), fallback: () => {}});
    })()""".replace("__SKIN__", json.dumps(_skin(_tube(R_OUT, R_IN)))).replace("__CHECK__", json.dumps(_check(None)))
                  .replace("__FACE__", json.dumps(_face())).replace("__S__", sid), timeout=60)
    live.wait_for(f"window._vdbg && window._vdbg['gstage:{sid}']", timeout=90, what="the stage to initialise")


def _click(live, sid: str, move: float = 0.0) -> None:
    """A press on the spot and its release `move` px away, sent where the user's mouse would be."""
    live.evaluate(f"""(() => {{
      const at = window._vdbg['gstage:{sid}'].screenOf({json.dumps(SPOT)});
      const el = document.elementFromPoint(at[0], at[1]);
      const ev = (type, x, y) => el.dispatchEvent(new PointerEvent(type, {{bubbles: true, cancelable: true,
        clientX: x, clientY: y, button: 0, pointerId: 1, isPrimary: true}}));
      ev('pointerdown', at[0], at[1]); ev('pointerup', at[0] + {move}, at[1]);
    }})()""")
    live.settle(0.4)


def _state(live) -> dict:
    return live.evaluate("""(() => ({
      chat: window.__chips.items('chat').filter((i) => i.kind === 'face').map((i) => i.label),
      notes: window.__chips.items('notes').filter((i) => i.kind === 'face').map((i) => i.label),
      shown: [...document.querySelectorAll('.box-chips[data-box="chat"] .box-chip-face .box-chip-label')].map((e) => e.textContent),
      lit: window._vdbg.facepick.lit(), picked: window._vdbg.facepick.picked(), asked: window.__asked.length,
      faces: window.__chips.payload('chat').faces}))()""")


def test_a_click_on_the_settled_part_picks_the_face(live):
    sid = "facepick"
    _open(live, sid)
    # THE PANEL IS OPEN: a click on the part is what it always was - no face asked for, nothing on screen
    _click(live, sid)
    s = _state(live)
    assert s["asked"] == 0 and s["chat"] == [] and s["lit"] == 0
    _proceed(live, sid)
    live.wait_for(f"document.getElementById('gstage-{sid}').classList.contains('iv-plain')", timeout=10,
                  what="the stage to settle")
    # A DRAG turns the model: no pick
    _click(live, sid, move=30)
    assert _state(live)["asked"] == 0
    # A CLICK: the face is asked for at the point on the part, lit, and one chip goes under the chat box
    _click(live, sid)
    live.wait_for("window._vdbg.facepick.lit() === 1", timeout=10, what="the face to be lit")
    s = _state(live)
    assert s["asked"] == 1 and s["chat"] == ["Face 12 · round Ø15 mm"] and s["shown"] == ["Face 12 · round Ø15 mm"]
    assert s["picked"] == ["Face_12"] and s["faces"] == [{"id": "Face_12", "label": "Face 12 · round Ø15 mm"}]
    body = live.evaluate("window.__asked[0]")
    assert all(abs(a - b) < 0.002 for a, b in zip(body["point_m"], SPOT)), body
    assert len(body["normal"]) == 3 and sum(n * n for n in body["normal"]) == pytest.approx(1, abs=1e-3)
    # THE SAME FACE AGAIN drops it, chip and light
    _click(live, sid)
    s = _state(live)
    assert s["chat"] == [] and s["lit"] == 0 and s["shown"] == []
    # picked again, then its ✕
    _click(live, sid)
    live.wait_for("window._vdbg.facepick.lit() === 1", timeout=10, what="the face lit again")
    live.evaluate("document.querySelector('.box-chips[data-box=\"chat\"] .box-chip-face .box-chip-x').click()")
    s = _state(live)
    assert s["chat"] == [] and s["lit"] == 0
    # A CLICK ON NO FACE does nothing
    live.evaluate("window.__noFace = true")
    _click(live, sid)
    s = _state(live)
    assert s["asked"] == 4 and s["chat"] == [] and s["lit"] == 0      # pick, drop, pick, this one
    assert_clean(live, "a face picked on the settled stage")


def test_the_notes_box_the_cap_and_the_send(live):
    sid = "facepick2"
    _open(live, sid)
    _proceed(live, sid)
    live.wait_for(f"document.getElementById('gstage-{sid}').classList.contains('iv-plain')", timeout=10,
                  what="the stage to settle")
    # THE NOTES BOX was the last one used: the chip goes under it
    live.evaluate("window.__chips.setActive('notes')")
    _click(live, sid)
    live.wait_for("window._vdbg.facepick.lit() === 1", timeout=10, what="the face lit")
    s = _state(live)
    assert s["notes"] == ["Face 12 · round Ø15 mm"] and s["chat"] == []
    # SENT: the box's chips are cleared without a word to them - the light goes with them
    live.evaluate("window.__chips.clear('notes')")
    live.wait_for("window._vdbg.facepick.lit() === 0", timeout=5, what="the light to go with the sent chip")
    # AT MOST 20 faces under one box
    live.evaluate("window.__chips.setActive('chat'); window.__seq = 1")
    hit = {"point": SPOT, "normal": [0, -S, S]}
    got = live.evaluate(f"""(async () => {{ const out = [];
      for (let k = 0; k < 21; k++) out.push(await window._vdbg.facepick.pickAt({json.dumps(hit)}));
      return out; }})()""", timeout=60)
    assert sum(1 for g in got if g and g.get("picked")) == 20 and got[-1] == {"full": True}
    assert len(_state(live)["chat"]) == 20
    # THE RUN STARTS: the viewer goes, and every picked face and its chip with it
    live.evaluate(f"window.__stages['{sid}'].release()")
    s = _state(live)
    assert s["chat"] == [] and s["lit"] == 0 and s["picked"] == []
    assert_clean(live, "face picks under the notes box, the cap and the send")
