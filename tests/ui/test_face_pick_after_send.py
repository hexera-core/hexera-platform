# Responsibility: Verify in a real browser that face picking stays on for the whole plan step (NOTE-17, NOTE-18): pick
#   two faces, send a requirement, and the next click on the model still picks a face - after a reply that came in the
#   send's own answer and after one that came later with the poll, with the plan bar's list opened and closed, after
#   the window is resized, and while "Reading…" still stands (that pick stays for the next message). The sent faces
#   stay marked on the model in the quiet colour (--pick-kept), the faces picked for the next message in the accent;
#   a mark goes with its requirement and with the run. One plan card, where it was. A reload at the plan step draws
#   the part again, settled, so its faces can be picked.
# Boundaries: the page's own wiring (ui/js/main.js); the server is stood in for in the page, like the real one: the
#   skin in metres, the parts model in MILLIMETRES (the measured surface of a CAD file), and faces/at answers only a
#   point on the part in metres. It reads screen pixels through the DevTools screenshot.
from __future__ import annotations

import base64
import io
import json
import math
import os

import numpy as np
import pytest
from conftest import assert_clean
from test_fea_stage_mesh_as import _check, _proceed
from test_geometry_stage_open_ends import LENGTH, ORIGIN, R_IN, R_OUT, _skin, _tube

pytestmark = pytest.mark.ui

OX, OY, OZ = ORIGIN
PLAN_LINE = "Here's the plan. Tell me any mesh requirements here, then say go or press Proceed."
BRIEF = {"purpose": "structural", "purpose_label": "Structural (FEA)", "engine": "gmsh", "engine_label": "Gmsh",
         "label": "Tube static stress", "user_notes": [], "proceed_ask": PLAN_LINE,
         "checks": [{"label": "Closed solid", "status": "pass"}]}
SHOTS = "/home/areen/intake_pick_lab/shots"
ODD: list = []        # the last look's samples that were not the colour of their kind (for the failure message)
ORDER = ["Part_1", *[f"Feature_{k}" for k in range(1, 9)]]   # the part, and each face as an area inside it


def _b64(a, t) -> str:
    return base64.b64encode(np.ascontiguousarray(np.asarray(a, dtype=t)).ravel().tobytes()).decode("ascii")


def _face_of(c, n) -> int:
    """The face (1..8) a point on the tube is on: the outer wall in four quarters (1-4), the bore in two halves (5, 6),
    the ends (7 at the start, 8 at the far end) - a CAD file's faces of a tube."""
    if abs(n[0]) > 0.9:
        return 7 if c[0] < OX + LENGTH / 2 else 8
    a = math.atan2(c[2] - OZ, c[1] - OY) % (2 * math.pi)
    if math.hypot(c[1] - OY, c[2] - OZ) > (R_IN + R_OUT) / 2:
        return 1 + min(3, int(a // (math.pi / 2)))
    return 5 + (a >= math.pi)


def _highlight(k: int) -> dict:
    """The server's own triangles of face k (metres)."""
    pts, tris = [], []
    if k <= 6:
        r, a0, span, m = (R_OUT, (k - 1) * math.pi / 2, math.pi / 2, 24) if k <= 4 else (R_IN, (k - 5) * math.pi, math.pi, 48)
        for j in range(m + 1):
            a = a0 + span * j / m
            pts += [(OX, OY + r * math.cos(a), OZ + r * math.sin(a)), (OX + LENGTH, OY + r * math.cos(a), OZ + r * math.sin(a))]
        for j in range(m):
            i = 2 * j
            tris += [(i, i + 1, i + 3), (i, i + 3, i + 2)]
    else:
        x = OX if k == 7 else OX + LENGTH
        for j in range(48):
            a = 2 * math.pi * j / 48
            pts += [(x, OY + R_IN * math.cos(a), OZ + R_IN * math.sin(a)), (x, OY + R_OUT * math.cos(a), OZ + R_OUT * math.sin(a))]
        for j in range(48):
            i, n = 2 * j, 2 * ((j + 1) % 48)
            tris += [(i, i + 1, n + 1), (i, n + 1, n)]
    return {"points_b64": _b64(pts, np.float32), "tris_b64": _b64(tris, np.uint32), "units": "m"}


FACES = {f"Face_{k}": {"id": f"Face_{k}", "label": f"Face {k}", "highlight": _highlight(k)} for k in range(1, 9)}


def _model() -> dict:
    """The parts model as the server stores it for a CAD part: the measured surface, in millimetres, each triangle
    in the area (face) it is on."""
    tris = _tube(R_OUT, R_IN).reshape(-1, 3, 3)
    pts = (tris * 1000.0).reshape(-1, 3)
    nrm, part = [], []
    for t in tris:
        n = np.cross(t[1] - t[0], t[2] - t[0])
        n = n / (np.linalg.norm(n) or 1)
        nrm += [n, n, n]
        part.append(1 + _face_of(t.mean(axis=0), n))       # the slot: Feature_k is order[k], drawn as k + 1
    return {"kind": "parts_model", "order": ORDER, "points_b64": _b64(pts, np.float32), "normals_b64": _b64(nrm, np.float32),
            "tris_b64": _b64(np.arange(len(pts)), np.uint32), "part_b64": _b64(part, np.uint16),
            "edges_b64": _b64([], np.uint32), "spots": [], "triangles": len(tris)}


SETUP = """
  const S = await import('/static/js/core/state.js');
  const C = await import('/static/js/shell/composer.js');
  const { Stage } = await import('/static/js/render/stage.js');
  const chips = await import('/static/js/shell/chips.js');
  const IV = await import('/static/js/viewer/intake_view.js');
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const SID = '__S__', SKIN = __SKIN__, CHECK = __CHECK__, FACES = __FACES__, MODEL = __MODEL__, BRIEF = __BRIEF__;
  const O = __O__, L = __L__, RI = __RI__, RO = __RO__;
  const ROWS = [];
  const done = () => ({status: 'done', rows: ROWS.slice(), unsure: [], model: true, order: __ORDER__,
                       parts: [{id: 'Part_1', name: ''}], inside: Object.fromEntries(__ORDER__.slice(1).map(f => [f, 'Part_1']))});
  const brief = (pm) => ({...BRIEF, part_matches: pm || (ROWS.length ? done() : null)});
  // THE SERVER'S faces/at: the face a point on the part is on, in metres - a point anywhere else is on no face
  const faceAt = (p, n) => {
    const r = Math.hypot(p[1] - O[1], p[2] - O[2]), a = ((Math.atan2(p[2] - O[2], p[1] - O[1]) % (2 * Math.PI)) + 2 * Math.PI) % (2 * Math.PI);
    if (p[0] < O[0] - 0.002 || p[0] > O[0] + L + 0.002) return null;
    if (n && Math.abs(n[0]) > 0.9 && r > RI - 0.003 && r < RO + 0.003)
      return Math.abs(p[0] - O[0]) < 0.003 ? 'Face_7' : Math.abs(p[0] - O[0] - L) < 0.003 ? 'Face_8' : null;
    if (Math.abs(r - RO) < 0.003) return 'Face_' + (1 + Math.min(3, Math.floor(a / (Math.PI / 2))));
    if (Math.abs(r - RI) < 0.003) return 'Face_' + (5 + (a >= Math.PI ? 1 : 0));
    if (r > RI - 0.003 && r < RO + 0.003) return Math.abs(p[0] - O[0]) < 0.003 ? 'Face_7' : Math.abs(p[0] - O[0] - L) < 0.003 ? 'Face_8' : null;
    return null;
  };
  window.__calls = []; window.__asked = []; window.__parts = [];
  const real = window.fetch.bind(window);
  const json = (x) => new Response(JSON.stringify(x), {status: 200, headers: {'Content-Type': 'application/json'}});
  window.fetch = async (u, o = {}) => {
    const s = String(u);
    if (s.includes('/check/skin')) return json(SKIN);
    if (s.includes('/check/faces/at')) { const b = JSON.parse(o.body); window.__asked.push(b);
      const id = faceAt(b.point_m, b.normal); return json(id ? FACES[id] : {id: null}); }
    if (s.includes('/check/parts/model')) return json(MODEL);
    if (s.includes('/check/parts')) {
      const next = window.__parts.length > 1 ? window.__parts.shift() : window.__parts[0];
      if (next && next.rows) ROWS.splice(0, ROWS.length, ...next.rows);
      return json(next || done());
    }
    if (s === '/api/v1/chat/message') {
      const body = JSON.parse(o.body), t = body.content; window.__calls.push(body);
      if (t === 'static stress') return json({session_id: SID, reply: BRIEF.proceed_ask, brief: brief()});
      if (/drop the first/.test(t)) { ROWS.shift(); return json({session_id: SID, reply: 'Done, that one is gone.', brief: brief()}); }
      const row = {phrase: ROWS.length, quote: t, words: t, size: 'finer', parts: [], picked: true, faces: body.faces};
      if (/later/.test(t)) {
        // A READING SLOWER THAN THE SEND WAITS: "Reading…" stays, the reply comes with the poll
        window.__parts = [{status: 'reading'}, {...done(), rows: [...ROWS, row], reply: 'Noted, finer there.'}];
        return json({session_id: SID, reply: '', reply_later: true, awaiting_confirmation: true, brief: brief({...done(), status: 'reading'})});
      }
      await sleep(window.__slow || 500);
      ROWS.push(row);
      return json({session_id: SID, reply: 'Finer mesh is set on the faces you picked.', brief: brief(), awaiting_confirmation: true});
    }
    if (s.startsWith('/api/')) return json({});
    return real(u, o);
  };
  window.__confirmed = {};
  const { openGeometryStage } = await import('/static/js/viewer/geometry_stage.js');
  window.__stage = await openGeometryStage(SID, CHECK, async (b) => { window.__confirmed[SID] = b; return {message: 'ok'}; },
                                           {anchorEl: document.getElementById('stage'), fallback: () => {}});
  C.configureComposer({onJobStarted: (id) => { window.__started = id; }});
  S.set.sessionId(SID);
  const inp = document.getElementById('chat-input');
  const type = async (t) => { inp.value = t; inp.dispatchEvent(new Event('input'));
    inp.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true})); };
  // where a face shows on the model drawn on top: the parts model when it is up, else the stage
  const pv = () => Stage._pm3d;
  // (on the parts model: the sample of that face nearest the middle of what is seen of it, off its id picture)
  const where = (id, p) => { if (!pv()) return window._vdbg['gstage:' + SID].screenOf(p);
    const ss = samples().filter(s => s[2] === id); if (!ss.length) return null;
    const mx = ss.reduce((a, s) => a + s[0], 0) / ss.length, my = ss.reduce((a, s) => a + s[1], 0) / ss.length;
    ss.sort((a, b) => Math.hypot(a[0] - mx, a[1] - my) - Math.hypot(b[0] - mx, b[1] - my)); return ss[0]; };
  // the faces under the box, the faces lit and marked
  const state = () => ({chips: chips.items('chat').filter(i => i.kind === 'face').map(i => i.value.id),
                        picked: window._vdbg.facepick.picked(), marked: window._vdbg.facepick.marked ? window._vdbg.facepick.marked() : [],
                        ready: window._vdbg.facepick.ready(), cards: document.querySelectorAll('#cc .req-card').length,
                        pending: document.querySelectorAll('#cc .im-pending').length});
  // the samples of the parts model each well inside one face (read off its id picture): [x, y, faceId]
  const samples = () => { const v = pv(), host = document.querySelector('.iv-parts'), r = host.getBoundingClientRect();
    const STEP = 5, cols = Math.floor(r.width / STEP), rows = Math.floor(r.height / STEP), g = [];
    for (let j = 0; j < rows; j++) for (let i = 0; i < cols; i++) {
      const x = r.left + (i + 0.5) * STEP, y = r.top + (j + 0.5) * STEP;
      // the canvas itself: not the hover tip or a tag over it
      const e = document.elementFromPoint(x, y), s = e && e.tagName === 'CANVAS' && e.closest('.iv-parts') ? v.debug.slotAt(x, y) : -1;
      g.push(s > 1 ? 'Face_' + (s - 1) : null); }
    const out = [];
    for (let j = 1; j < rows - 1; j++) for (let i = 1; i < cols - 1; i++) { const k = g[j * cols + i];
      if (k && [[-1, 0], [1, 0], [0, -1], [0, 1]].every(([a, b]) => g[(j + b) * cols + i + a] === k))
        out.push([r.left + (i + 0.5) * STEP, r.top + (j + 0.5) * STEP, k]); }
    return out; };
  window.__t = {C, Stage, chips, IV, sleep, type, where, state, samples, pv};
"""


def _setup() -> str:
    return (SETUP.replace("__SKIN__", json.dumps(_skin(_tube(R_OUT, R_IN)))).replace("__CHECK__", json.dumps(_check(None)))
            .replace("__FACES__", json.dumps(FACES)).replace("__MODEL__", json.dumps(_model()))
            .replace("__BRIEF__", json.dumps(BRIEF)).replace("__ORDER__", json.dumps(ORDER))
            .replace("__O__", json.dumps(list(ORIGIN))).replace("__L__", repr(LENGTH))
            .replace("__RI__", repr(R_IN)).replace("__RO__", repr(R_OUT)))


def _size(live, width: int, height: int) -> None:
    live._send("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})
    live.evaluate("new Promise(r => setTimeout(r, 400))")


def _outer(a: float) -> list:
    return [OX + LENGTH / 2, OY + R_OUT * math.cos(a), OZ + R_OUT * math.sin(a)]


def _pick(live, face: str, point=None) -> None:
    """A real click (the mouse, through the page) on `face` where it shows on the model on top; its chip must come."""
    before = live.evaluate("window.__t.state().chips.length")
    xy = live.evaluate(f"window.__t.where({json.dumps(face)}, {json.dumps(point)})")
    assert xy, f"{face} is seen on the model"
    live.click(xy[0], xy[1])
    live.wait_for(f"window.__t.state().chips.length === {before + 1}", timeout=10, what=f"a chip for the face clicked ({face})")


def _wait_view(live) -> None:
    live.wait_for("window.__t.pv() && window.__t.pv().debug.ready()", timeout=40, what="the parts model drawn and its ids taken")


def _shot(live, name: str) -> None:
    data = live._send("Page.captureScreenshot", {"format": "png"})["data"]
    os.makedirs(SHOTS, exist_ok=True)
    with open(os.path.join(SHOTS, name), "wb") as fh:
        fh.write(base64.b64decode(data))


def _colours(live) -> tuple:
    rgb = live.evaluate("""(async () => { const { themeRgb } = await import('/static/js/viewer/toolbar.js');
      return [themeRgb('--accent', [0, 0, 0]), themeRgb('--pick-kept', [0, 0, 0])]; })()""")
    return tuple(tuple(int(round(255 * v)) for v in c) for c in rgb)


def _kind(px, accent) -> str:
    """'bright' (the accent: red >= 70% of it, the rest within 35 of its own), 'quiet' (red leads clearly, but the
    other two well above the accent's), or 'grey' (no lead: the part)."""
    r, g, b = px
    lead = r - max(g, b)
    if r >= 0.7 * accent[0] and max(g, b) <= max(accent[1], accent[2]) + 35:
        return "bright"
    if lead >= 25 and min(g, b) > max(accent[1], accent[2]) + 35:
        return "quiet"
    return "grey" if abs(lead) < 18 else "other"


def _look(live, view: str | None) -> dict:
    """The parts model from `view` (None: as it is): per face, how many of its samples are bright, quiet, grey."""
    if view:
        # the parts model is drawn in its own millimetres
        pos, foc = {"front": ([OX + LENGTH / 2, OY - 0.55, OZ + 0.35], [OX + LENGTH / 2, OY, OZ]),
                    "end": ([OX - 0.35, OY - 0.2, OZ + 0.25], [OX + LENGTH / 2, OY, OZ])}[view]
        pos, foc = [1000 * v for v in pos], [1000 * v for v in foc]
        live.evaluate(f"""(async () => {{ const v = window.__t.pv(), cam = v.debug.cam();
          cam.setPosition(...{json.dumps(pos)}); cam.setFocalPoint(...{json.dumps(foc)}); cam.setViewUp(0, 0, 1);
          cam.setClippingRange(5, 5000); v.debug.render(); }})()""")
    # the mouse off the model (its hover tip would cover a face); the id picture is taken again after any move of
    # the view (a resize as the page settles is one): read it then
    live._send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 5, "y": 5})
    live.settle(0.4)
    _wait_view(live)
    pts = live.evaluate("window.__t.samples()")
    ODD.clear()
    from PIL import Image
    img = Image.open(io.BytesIO(base64.b64decode(live._send("Page.captureScreenshot", {"format": "png"})["data"]))).convert("RGB")
    dpr = float(live.evaluate("window.devicePixelRatio || 1"))
    accent, _ = _colours(live)
    out: dict = {}
    for x, y, f in pts:
        px = img.getpixel((int(x * dpr), int(y * dpr)))
        k = _kind(px, accent)
        out.setdefault(f, {"bright": 0, "quiet": 0, "grey": 0, "other": 0})[k] += 1
        if k in ("grey", "other"):
            ODD.append((f, k, x, y, px))
    return out


def _all(seen: dict, faces, kind: str) -> None:
    for f in faces:
        c = seen.get(f)
        assert c and c[kind] > 20 and sum(c.values()) == c[kind], (f, kind, seen, [o for o in ODD if o[0] == f][:4])


def test_picking_stays_on_after_each_requirement_and_sent_faces_stay_marked(live):
    _size(live, 1280, 860)
    live.evaluate("(async () => {" + _setup().replace("__S__", "pk-send") + "})()", timeout=90)
    live.wait_for("window._vdbg && window._vdbg['gstage:pk-send']", timeout=90, what="the stage")
    _proceed(live, "pk-send")
    live.wait_for("window._vdbg.facepick.ready()", timeout=10, what="the stage to settle")
    live.evaluate("window.__t.C.say('static stress')", timeout=30)
    live.wait_for("document.getElementById('plan-bar')", timeout=10, what="the plan bar")
    # 1) two faces on the stage, one requirement; its reply in the send's own answer
    _pick(live, "Face_1", _outer(0.4 * math.pi))
    _pick(live, "Face_2", _outer(0.75 * math.pi))
    live.evaluate("window.__t.type('I want a finer mesh in these areas')")
    live.wait_for("window.__t.state().pending === 0 && window.__calls.length === 2", timeout=15, what="the first reply")
    _wait_view(live)
    s = live.evaluate("window.__t.state()")
    assert s["chips"] == [] and s["ready"] and s["cards"] == 1, s
    assert live.evaluate("document.querySelector('#plan-bar .pb-count').textContent.trim()") == "1 requirement"
    # THE NEXT CLICK ON THE MODEL PICKS A FACE (the live fault: it only turned the model), and a second click drops it
    _pick(live, "Face_3")
    assert live.evaluate("window.__t.state().picked") == ["Face_3"]
    xy = live.evaluate("window.__t.where('Face_3')")
    live.click(xy[0], xy[1])
    live.wait_for("window.__t.state().chips.length === 0", timeout=10, what="the face dropped by a second click")
    assert sorted(live.evaluate("window.__t.state().marked")) == ["Face_1", "Face_2"]
    # THE SENT FACES STAY MARKED, quiet, from two angles; the rest is grey
    for view in ("front", "end"):
        seen = _look(live, view)
        _all(seen, ["Face_1", "Face_2"] if view == "front" else ["Face_2"], "quiet")
        _all(seen, [f for f in ("Face_3", "Face_7") if f in seen], "grey")
    _look(live, "front")
    _shot(live, "01_after_first_send_marks.png")
    # 2) THE NEXT CLICK STILL PICKS: with the bar's list open, and closed again
    live.evaluate("document.querySelector('#plan-bar .pb-count').click()")
    _pick(live, "Face_3")
    seen = _look(live, None)
    _all(seen, ["Face_3"], "bright")
    _all(seen, ["Face_1", "Face_2"], "quiet")
    _shot(live, "02_second_pick_bright_beside_marks.png")
    live.evaluate("document.querySelector('#plan-bar .pb-count').click()")
    _look(live, "end")                    # the near end and the bore in view
    # 3) a requirement whose reply comes later, with the poll: a pick while "Reading…" stands stays for the next one
    live.evaluate("window.__t.type('2 mm here, later is fine')")
    live.wait_for("window.__t.state().pending === 1 && window.__t.state().chips.length === 0", timeout=10, what="Reading…")
    _pick(live, "Face_7")
    assert live.evaluate("window.__t.state().pending") == 1, "picked while Reading… stood"
    live.wait_for("window.__t.state().pending === 0", timeout=30, what="the reply with the poll")
    s = live.evaluate("window.__t.state()")
    assert s["chips"] == ["Face_7"] and sorted(s["marked"]) == ["Face_1", "Face_2", "Face_3"] and s["cards"] == 1, s
    # 4) the window resized: still picking; then a reply in the send's answer, and a pick while it is on its way
    _size(live, 1100, 820)
    _wait_view(live)
    _pick(live, "Face_6")
    live.evaluate("window.__slow = 1500; window.__t.type('and finer on these two')")
    live.wait_for("window.__t.state().pending === 1", timeout=10, what="Reading…")
    _pick(live, "Face_2")                    # a marked face, picked again for the next message
    live.wait_for("window.__t.state().pending === 0", timeout=15, what="the third reply")
    s = live.evaluate("window.__t.state()")
    assert window_sent(live) == ["Face_7", "Face_6"], window_sent(live)
    assert s["chips"] == ["Face_2"] and s["picked"] == ["Face_2"], s
    assert sorted(s["marked"]) == ["Face_1", "Face_2", "Face_3", "Face_6", "Face_7"] and s["cards"] == 1, s
    seen = _look(live, "front")
    _all(seen, ["Face_2"], "bright")              # picked again: the accent wins over its mark
    _all(seen, ["Face_1", "Face_3"], "quiet")
    live.evaluate("document.querySelector('#plan-bar .pb-count').click()")
    live.settle(0.3)
    _shot(live, "03_bar_open.png")
    live.evaluate("document.querySelector('#plan-bar .pb-count').click()")
    # 5) A REQUIREMENT GONE: its faces' marks go with it (the reading no longer holds the first row)
    live.evaluate("document.querySelector('.box-chips[data-box=\"chat\"] .box-chip-face .box-chip-x').click()")
    live.evaluate("window.__slow = 300; window.__t.type('drop the first one')")
    live.wait_for("window.__t.state().pending === 0 && window.__calls.length === 5", timeout=15, what="the reply")
    s = live.evaluate("window.__t.state()")
    assert sorted(s["marked"]) == ["Face_3", "Face_6", "Face_7"], s
    seen = _look(live, "front")
    _all(seen, ["Face_1", "Face_2"], "grey")
    _all(seen, ["Face_3"], "quiet")
    # 6) THE RUN STARTS (the intake's viewer goes, main.js attachJob): no mark, no light is left
    live.evaluate("window.__t.IV.endIntake()")
    s = live.evaluate("window.__t.state()")
    assert s["marked"] == [] and s["picked"] == [] and s["chips"] == [] and not s["ready"], s
    assert_clean(live, "picking after each requirement")


def window_sent(live) -> list:
    return [f["id"] for f in live.evaluate("window.__calls[window.__calls.length - 1].faces")]


def test_a_reload_at_the_plan_step_draws_the_part_again_and_its_faces_can_be_picked(live):
    _size(live, 1280, 860)
    history = {"session_id": "pk-reload", "job_id": None, "geometry_filename": "tube.step", "choices": [],
               "messages": [{"role": "assistant", "content": PLAN_LINE},
                            {"role": "user", "content": "I want a finer mesh in these areas",
                             "faces": [{"id": "Face_1", "label": "Face 1"}]},
                            {"role": "assistant", "content": "Finer mesh is set on the face you picked."}]}
    out = live.evaluate("(async () => {" + _setup().replace("__S__", "pk-reload").replace(
        "window.__stage = await openGeometryStage", "window.__stage = null && openGeometryStage") + """
      ROWS.push({phrase: 0, quote: 'x', words: 'I want a finer mesh in these areas', size: 'finer', parts: [], picked: true,
                 faces: [{id: 'Face_1', label: 'Face 1'}]});
      const H = __H__;
      const { intakeOrder } = await import('/static/js/core/conversation.js');
      const stubbed = window.fetch;
      // the check, answered before the reload
      window.fetch = async (u, o) => String(u).endsWith('/geometry/pk-reload/check')
        ? new Response(JSON.stringify({...CHECK, route: 'fea', confirmed: true}), {status: 200, headers: {'Content-Type': 'application/json'}})
        : stubbed(u, o);
      Stage.mount();
      for (const item of intakeOrder(H.messages)) Stage.chat(item.role, item.text);
      C.resumeSession(H.session_id, {...H, brief: brief()});
      return true;
    })()""".replace("__H__", json.dumps(history)), timeout=90)
    assert out
    live.wait_for("window._vdbg && window._vdbg['gstage:pk-reload']", timeout=90, what="the stage drawn again")
    live.wait_for("window._vdbg.facepick.ready()", timeout=20, what="the stage settled again")
    _wait_view(live)
    # ONE CARD, under the plan line where it stood live - not under the last reply
    order = live.evaluate("""[...document.querySelectorAll('#cc > .im')].map(e => e.querySelector('.req-card') ? 'card'
      : (e.querySelector('.txt, .bub') || {}).textContent.trim().slice(0, 12))""")
    assert order.count("card") == 1 and order.index("card") == 1, order
    _pick(live, "Face_2")
    s = live.evaluate("window.__t.state()")
    assert s["chips"] == ["Face_2"] and s["picked"] == ["Face_2"], s
    assert_clean(live, "a reload at the plan step")
