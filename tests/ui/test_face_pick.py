# Responsibility: Verify face picking on the settled intake model in a real browser (NOTE-17): no pick while the stage
# panel is open; after Proceed a plain click asks the server for the face there, lights it and puts one chip
# "Face 12 · round Ø15 mm ✕" under the box used last; a second click on it or its ✕ drops it; a drag is not a click; a
# click on no face does nothing; at most 20 faces, the 21st told in one line; the send's chip clear takes the light with
# it; a face and a file go in one message, a refused send keeps both lit and typed; a face chip looks the same under
# the box, in the user's bubble and on a saved note.
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
    # the 21st is told in one plain line under the box, never ignored silently
    assert live.evaluate("document.querySelector('#input-bar > .box-msg').textContent") == "20 faces is the most."
    # THE RUN STARTS: the viewer goes, and every picked face and its chip with it
    live.evaluate(f"window.__stages['{sid}'].release()")
    s = _state(live)
    assert s["chat"] == [] and s["lit"] == 0 and s["picked"] == []
    assert_clean(live, "face picks under the notes box, the cap and the send")


def test_a_face_and_a_file_go_together_a_refused_send_keeps_both_and_the_face_looks_the_same_everywhere(live):
    sid = "facepick3"
    _open(live, sid)
    _proceed(live, sid)
    live.wait_for(f"document.getElementById('gstage-{sid}').classList.contains('iv-plain')", timeout=10,
                  what="the stage to settle")
    _click(live, sid)
    live.wait_for("window._vdbg.facepick.lit() === 1", timeout=10, what="the face lit")
    out = live.evaluate("""(async () => {
      const S = await import('/static/js/core/state.js');
      const C = await import('/static/js/shell/composer.js');
      const { Stage } = await import('/static/js/render/stage.js');
      const { carried } = await import('/static/js/core/conversation.js');
      const chips = window.__chips, sleep = (ms) => new Promise(r => setTimeout(r, ms));
      const stubbed = window.fetch, sent = [];
      let refuse = true;
      window.fetch = async (u, o = {}) => {
        if (String(u) !== '/api/v1/chat/message') return stubbed(u, o);
        sent.push(JSON.parse(o.body));
        return refuse
          ? new Response(JSON.stringify({detail: 'Face 12 is not on the model. Click the face on the model again, then send.'}),
                         {status: 422, headers: {'Content-Type': 'application/json'}})
          : new Response(JSON.stringify({reply: 'Noted.', choices: []}), {status: 200, headers: {'Content-Type': 'application/json'}});
      };
      S.set.sessionId('__S__'); C.enableInput();
      // a picture already uploaded (as shell/attach.js holds one)
      chips.add('chat', {kind: 'image', label: '', value: 'a100000000000000',
                         row: {id: 'a100000000000000', kind: 'image', name: 'bore.png'}});
      const inp = document.getElementById('chat-input');
      const look = (root) => [...root.querySelectorAll('.box-chip-face')].map(c => ({
        label: c.querySelector('.box-chip-label').textContent, dot: getComputedStyle(c, '::before').width,
        h: c.getBoundingClientRect().height}));
      const under = () => look(document.querySelector('.box-chips[data-box="chat"]'));
      const before = under();
      inp.value = '2 mm here';
      document.getElementById('send-btn').click();
      await sleep(300);
      const refused = {kinds: chips.items('chat').map(i => i.kind), words: inp.value, picked: window._vdbg.facepick.picked(),
                       lit: window._vdbg.facepick.lit(), line: document.querySelector('#input-bar > .box-msg').textContent,
                       bubbles: document.querySelectorAll('#cc .im.user').length};
      refuse = false;
      document.getElementById('send-btn').click();
      await sleep(300);
      const bubble = document.querySelector('#cc .im.user');
      const done = {chips: chips.items('chat').length, lit: window._vdbg.facepick.lit(), picked: window._vdbg.facepick.picked(),
                    inBubble: bubble ? look(bubble) : [], pic: !!(bubble && bubble.querySelector('.att-img'))};
      // RELOADED: the stored turn carries both, and draws both
      const turn = carried({role: 'user', content: '2 mm here', attachments: [{id: 'a100000000000000', kind: 'image', name: 'bore.png'}],
                            faces: [{id: 'Face_12', label: 'Face 12 · round Ø15 mm'}]});
      // THE CHECKLIST ROW for words sent with a face shows the same chip (NOTE-18: the notes list is gone)
      Stage.brief({purpose: 'structural', engine: 'gmsh', user_notes: [{text: '2 mm here', attachments: [],
                   faces: [{id: 'Face_12', label: 'Face 12 · round Ø15 mm'}]}],
                   part_matches: {status: 'done', unsure: [], rows: [{phrase: 0, quote: '2 mm here', words: '2 mm here',
                   size: '2 mm', parts: [], picked: true, faces: [{id: 'Face_12', label: 'Face 12 · round Ø15 mm'}]}]}}, null);
      await sleep(0);
      const note = document.querySelector('.rc-pm-item.face');
      return {before, refused, sent, done, turn, onNote: note ? look(note) : []};
    })()""".replace("__S__", sid), timeout=60)
    face = {"id": "Face_12", "label": "Face 12 · round Ø15 mm"}
    # REFUSED: the face, the file and the words all stay; the face stays lit; the server's line under the box
    r = out["refused"]
    assert sorted(r["kinds"]) == ["face", "image"] and r["words"] == "2 mm here", out
    assert r["picked"] == ["Face_12"] and r["lit"] == 1 and r["bubbles"] == 0, out
    assert r["line"].startswith("Face 12 is not on the model."), out
    # SENT: one message carries the file and the face; then the chips and the light go
    assert out["sent"][-1]["attachments"] == ["a100000000000000"] and out["sent"][-1]["faces"] == [face], out
    assert out["done"]["chips"] == 0 and out["done"]["lit"] == 0 and out["done"]["picked"] == [], out
    assert out["done"]["pic"], out
    assert out["turn"]["attachments"][0]["id"] == "a100000000000000" and out["turn"]["faces"] == [face], out
    # ONE LOOK: the same chip, the same dot, the same height under the box, in the bubble and on a saved note
    (u,), (b,), (n,) = out["before"], out["done"]["inBubble"], out["onNote"]
    assert u["label"] == b["label"] == n["label"] == "Face 12 · round Ø15 mm", out
    assert u["dot"] == b["dot"] == n["dot"] == "7px" and u["h"] == b["h"] == n["h"], out


def test_the_checklist_shows_a_picked_face_and_a_click_in_the_parts_view_with_no_row_open_picks_a_face(live):
    from test_brief_part_matches import MODEL, SEEN_AT, _stage_under

    from meshpipeline.application import part_matches as PM
    # the server's row (NOTE-17's one rule, mesh_requirements.picked_for): the part reader tied the words to a hole
    # area (the live fault showed "a hole, 22 mm across", picked false); with the bore picked the row is the face's
    face = {"id": "Face_12", "label": "Face 12 · round Ø15 mm", "sig": {"area_m2": 0.0004, "centroid_m": [0, 0, 0]}}
    read = {"phrase": 1, "words": "2 mm elements here", "quote": "2 mm elements here", "parts": ["Feature_4"],
            "parts_text": "a hole, 22 mm across", "size": "2 mm", "picked": False, "lit": ["Feature_4"]}
    (served,) = PM.with_faces({"status": "done", "rows": [read], "unsure": [], "inside": {"Feature_4": "Part_1"}},
                              [{"text": "2 mm elements here", "source": "console", "faces": [face]}])["rows"]
    assert served["picked"] is True and served["parts"] == [] and served["faces"][0]["id"] == "Face_12", served
    _stage_under(live)
    live.evaluate("""(async () => {
      const { Stage } = await import('/static/js/render/stage.js');
      const { mountPartsView } = await import('/static/js/viewer/parts_view.js');
      const { partsHost, dropPartsHost } = await import('/static/js/viewer/intake_view.js');
      window.__chips = await import('/static/js/shell/chips.js');
      Stage.mount();
      __MODEL__
      done.rows.push(__SERVED__);
      const real = window.fetch;
      window.__asked = 0;
      window.fetch = (u, o) => String(u).includes('/check/faces/at')
        ? (window.__asked++, Promise.resolve(new Response(JSON.stringify({id: 'Face_4', label: 'Face 4 · flat 1 × 1 mm'}),
             {status: 200, headers: {'Content-Type': 'application/json'}})))
        : real(u, o);
      window.__picks = []; window.__view = null;
      const parts = {get: async () => done, model: async () => model,
                     pick: async (i, ids) => { window.__picks.push([i, ids]); return null; },
                     mount: async (h, m, o) => (window.__view = await mountPartsView(h, m, o)),
                     host: () => partsHost(), drop: (el) => dropPartsHost(el)};
      Stage.brief({purpose: 'structural', engine: 'gmsh', user_notes: [{text: 'the rail'}, {text: '2 mm elements here',
                   faces: [{id: 'Face_12', label: 'Face 12 · round Ø15 mm'}]}], part_matches: done}, async () => {}, parts);
    })()""".replace("__MODEL__", MODEL).replace("__SERVED__", json.dumps(served)), timeout=40)
    live.wait_for("window.__view && window.__view.debug.ready()", timeout=40, what="the parts model to be drawn")
    # THE CHECKLIST, BEFORE APPROVAL: the size typed with the pick shows the face's own chip, not a part
    card = live.evaluate("""(() => { const f = document.querySelector('.rc-pm-item.face');
      return f && {size: f.querySelector('.rc-pm-s').textContent, chip: f.querySelector('.box-chip-face .box-chip-label').textContent,
                   parts: f.querySelectorAll('.rc-pm-chip').length}; })()""")
    assert card == {"size": "2 mm", "chip": "Face 12 · round Ø15 mm", "parts": 0}, card
    live.evaluate("document.querySelector('.rc-pm-item.face .rc-pm-row').click()")
    assert live.evaluate("document.querySelectorAll('.rc-pm-item.active').length") == 0, "a face row is never re-tied"
    # NO ROW OPEN: a click on the parts view picks the face there (not a part for a row)
    assert live.evaluate("window._vdbg.facepick.ready()"), "the stage under the parts is settled"
    xy = live.evaluate(SEEN_AT.replace("__SLOT__", "2"))
    assert xy, "part 2 is seen on the model"
    live.click(xy[0], xy[1])
    live.wait_for("window._vdbg.facepick.picked().length === 1", timeout=10, what="the face to be picked")
    out = live.evaluate("""({asked: window.__asked, picks: window.__picks, active: document.querySelectorAll('.rc-pm-item.active').length,
      chat: window.__chips.items('chat').filter((i) => i.kind === 'face').map((i) => i.label)})""")
    assert out == {"asked": 1, "picks": [], "active": 0, "chat": ["Face 4 · flat 1 × 1 mm"]}, out
