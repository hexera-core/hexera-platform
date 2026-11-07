# Responsibility: Verify "Mesh as: Auto / Shell / Solid" on the FEA stage in a real browser (SHSTAGE-2..5): the
# stage opens while the walls are still measured and says so (and what Proceed does then); when the shell check
# lands, the panel follows in place - Auto's recommendation and its reason, what stays solid (S1) and where the
# plates do not meet (O1), each marked on the part, the legend in mm - and the mid-surface is drawn over the
# part coloured by its wall; picking Solid hides it, Shell shows it, and Proceed sends the pick. Words said in
# the chat open checked; a triangle file cannot be shelled. Auto's shell + solid (MIXED-4) draws the mid-surface and
# lists what is kept solid; Proceed sends Auto, which the server settles.
# Boundaries: the skin is built through the worker's own code from a tube made here; the FEA view and the drawing
# are the server's shapes, built here. Only the network is stood in for.
from __future__ import annotations

import base64
import json
import math

import numpy as np
import pytest
from conftest import assert_clean
from test_geometry_stage_open_ends import LENGTH, ORIGIN, R_IN, R_OUT, _skin, _tube

pytestmark = pytest.mark.ui


def _overlay() -> dict:
    """A mid-surface inside the tube's wall (mm): a cylinder at the wall's middle, the first half of it 8 mm
    thick and the rest 12 mm, and one open edge."""
    ox, oy, oz = (1000 * v for v in ORIGIN)
    r, n, L = 1000 * (R_OUT + R_IN) / 2, 24, 1000 * LENGTH
    pts, tris, walls = [], [], []
    for k in range(n + 1):
        a = 2 * math.pi * k / n
        pts += [(ox, oy + r * math.cos(a), oz + r * math.sin(a)), (ox + L, oy + r * math.cos(a), oz + r * math.sin(a))]
    for k in range(n):
        i = 2 * k
        tris += [(i, i + 1, i + 3), (i, i + 3, i + 2)]
        walls += [8.0 if k < n / 2 else 12.0] * 2
    b64 = lambda a, t: base64.b64encode(np.asarray(a, dtype=t).ravel().tobytes()).decode("ascii")  # noqa: E731
    return {"version": 1, "unit": "mm", "xyz": b64(pts, np.float32), "tri": b64(tris, np.uint32),
            "wall": b64(walls, np.float32), "triangles": len(tris), "t_min": 8.0, "t_max": 12.0,
            "open_at_mm": [[ox + L / 2, oy + r, oz]], "solid_at_mm": {}}


OPTIONS = [{"key": "auto", "label": "Auto (recommended)"},
           {"key": "shell", "label": "Shell", "words": "the mid-surface, meshed with plate elements"},
           {"key": "solid", "label": "Solid", "words": "the whole volume, meshed with tetrahedra"}]


def _shell(state: str, **kw) -> dict:
    base = {"question": "Mesh as", "state": state, "options": OPTIONS, "default": "auto", "said": "", "said_words": "",
            "recommend": "", "reason": "", "shell_ok": True, "shell_off": "",
            "measuring_words": "Measuring the walls to see if a shell mesh fits this part." if state == "measuring" else "",
            "early_proceed": "If you proceed before this is done, Auto meshes the part as a solid." if state == "measuring" else ""}
    base.update(kw)
    return base


def _ready() -> dict:
    ox, oy, oz = (1000 * v for v in ORIGIN)
    return _shell("ready", recommend="shell",
                  reason="95% of this part is plate 8 mm to 12 mm thick, so a shell mesh needs far fewer elements.",
                  share=0.95, t_min_mm=8.0, t_max_mm=12.0, bodies=[{"body": "Part_1", "verdict": "shell"}],
                  not_shelled=[
                      {"id": "S1", "kind": "solid", "words": "A thick region of the part (4 faces) stays solid: it is "
                       "30 mm thick or more, too thick for a shell.", "at_mm": [ox + 50, oy + 1000 * R_OUT, oz], "faces": []},
                      {"id": "", "kind": "open_head", "words": "The plates do not meet at 1 place: shown in red.",
                       "at_mm": None, "faces": []},
                      {"id": "O1", "kind": "open", "words": "Plates do not meet here (1 of 1).",
                       "at_mm": [ox + 150, oy + 45, oz], "faces": []}],
                  open_edges=1, open_at_mm=[[ox + 150, oy + 45, oz]], overlay=True, legend_mm=[8.0, 12.0], scale=1.0)


def _check(shell: dict, stl: bool = False) -> dict:
    return {"status": "ready", "named": True, "skin": True, "pictures": [], "proposal": {
        "part": "tube", "input_kind": "body-surface", "size_mm": [300.0, 100.0, 100.0], "unit": "mm",
        "unit_basis": "file", "notes": [], "openings": [], "faces": [], "holes": []},
        "fea": {"question": "Is this your part and are the units right?", "bodies": 1, "clean": True,
                "clean_summary": "watertight", "single_part_only": "", "can_proceed": True,
                "measure": {"measured": True, "basis": "cad", "thickness_min_mm": 8.0, "thickness_face": "Face_1",
                            "hot_spots": [], "counts": {}, "more": 0, "summary": "no hot spots", "notes": []},
                "shell": shell}}


def _open(live, sid: str, check: dict) -> None:
    live.evaluate("""(async () => {
      const SKIN = __SKIN__, CHECK = __CHECK__, OVERLAY = __OVERLAY__;
      const real = window.fetch.bind(window);
      const json = (x) => Promise.resolve(new Response(JSON.stringify(x), {status: 200, headers: {'Content-Type': 'application/json'}}));
      window.__overlayFetches = 0;
      window.fetch = (u, o) => String(u).includes('/geometry/__S__/check/skin') ? json(SKIN)
        : String(u).includes('/geometry/__S__/check/shell-surface') ? (window.__overlayFetches++, json(OVERLAY))
        : real(u, o);
      window.__confirmed = window.__confirmed || {};
      const { openGeometryStage } = await import('/static/js/viewer/geometry_stage.js');
      window.__stages = window.__stages || {};
      window.__stages['__S__'] = await openGeometryStage('__S__', CHECK,
        async (body) => { window.__confirmed['__S__'] = body; return {message: 'ok'}; },
        {anchorEl: document.getElementById('stage'), fallback: () => { window.__fellBack = true; }});
    })()""".replace("__SKIN__", json.dumps(_skin(_tube(R_OUT, R_IN)))).replace("__CHECK__", json.dumps(check))
                  .replace("__OVERLAY__", json.dumps(_overlay())).replace("__S__", sid), timeout=60)
    live.wait_for(f"window._vdbg && window._vdbg['gstage:{sid}']", timeout=90, what="the FEA stage to initialise")


def _state(live, sid: str) -> dict:
    return live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{sid}'), h = window._vdbg['gstage:{sid}'];
      const vis = (sel) => {{ const el = root.querySelector(sel); return !!el && !el.closest('[hidden]'); }};
      return {{radios: [...root.querySelectorAll('input[name="fea-mesh-as"]')].map((r) => [r.value, r.checked, r.disabled]),
               auto: (root.querySelector('.fea-meshas-auto') || {{}}).textContent || '',
               measuring: vis('.fea-meshas-measuring'), why: (root.querySelector('.fea-meshas-why') || {{}}).textContent || '',
               said: (root.querySelector('.fea-meshas-said') || {{}}).textContent || '',
               rows: [...root.querySelectorAll('.fea-shell-row')].map((r) => r.dataset.spot),
               list: vis('.fea-shell-list'), legend: vis('.fea-shell-legend'),
               head: (root.querySelector('.fea-shell-list .fea-spot-head') || {{}}).textContent || '',
               legendText: (root.querySelector('.fea-shell-legend-v') || {{}}).textContent || '',
               pins: [...root.querySelectorAll('.fea-shell-pin')].map((p) => p.dataset.spot),
               shell: h.shell(), fetches: window.__overlayFetches}}; }})()""")


def _pick(live, sid: str, key: str) -> None:
    live.evaluate(f"""(() => {{ const r = document.querySelector('#gstage-{sid} input[name="fea-mesh-as"][value="{key}"]');
      r.checked = true; r.dispatchEvent(new Event('change', {{bubbles: true}})); }})()""")


def test_mesh_as_follows_the_shell_check_and_draws_the_mid_surface(live):
    sid = "feameshas"
    _open(live, sid, _check(_shell("measuring")))
    s = _state(live, sid)
    assert s["radios"] == [["auto", True, False], ["shell", False, False], ["solid", False, False]]
    assert s["auto"] == "measuring the walls…" and s["measuring"]
    assert s["shell"] == {"actors": 0, "shown": False} and s["fetches"] == 0
    # the shell check lands: the panel follows in place, the mid-surface is fetched once and drawn
    live.evaluate(f"window.__stages['{sid}'].update({json.dumps(_check(_ready()))})")
    live.wait_for(f"window._vdbg['gstage:{sid}'].shell().shown", timeout=30, what="the mid-surface to be drawn")
    s = _state(live, sid)
    assert s["auto"] == "Shell" and not s["measuring"] and s["why"].startswith("95% of this part is plate")
    assert s["rows"] == ["S1", "O1"] and s["list"] and s["legend"] and s["legendText"] == "8 mm – 12 mm"
    assert s["pins"] == ["S1", "O1"]
    assert s["shell"]["actors"] == 2 and s["fetches"] == 1           # the surface and the open edges
    # Solid hides the mid-surface and the list; Shell shows them again
    _pick(live, sid, "solid")
    s = _state(live, sid)
    assert not s["shell"]["shown"] and not s["list"] and not s["legend"]
    _pick(live, sid, "shell")
    s = _state(live, sid)
    assert s["shell"]["shown"] and s["list"]
    # a later poll with the same view keeps the user's pick
    live.evaluate(f"window.__stages['{sid}'].update({json.dumps(_check(_ready()))})")
    assert _state(live, sid)["radios"][1] == ["shell", True, False]
    # Proceed sends the pick
    live.evaluate(f"document.querySelector('#gstage-{sid} .gc-proceed').click()")
    live.wait_for(f"window.__confirmed['{sid}']", timeout=20, what="the confirmation")
    assert live.evaluate(f"window.__confirmed['{sid}'].fea_mesh_as") == "shell"
    assert_clean(live, "the FEA stage's Mesh as")


def test_words_said_in_the_chat_open_checked_and_proceed_sends_them(live):
    sid = "feasaid"
    _open(live, sid, _check(_shell("measuring", default="shell", said="shell",
                                   said_words="You asked for a shell mesh in the chat.")))
    s = _state(live, sid)
    assert s["radios"][1] == ["shell", True, False] and s["said"] == "You asked for a shell mesh in the chat."
    live.evaluate(f"document.querySelector('#gstage-{sid} .gc-proceed').click()")
    live.wait_for(f"window.__confirmed['{sid}']", timeout=20, what="the confirmation")
    assert live.evaluate(f"window.__confirmed['{sid}'].fea_mesh_as") == "shell"
    assert_clean(live, "the FEA stage with shell words")


def test_a_triangle_file_cannot_be_shelled(live):
    sid = "feastl"
    off = "Shell meshes need a CAD file (STEP or IGES): this file is triangles."
    _open(live, sid, _check(_shell("unavailable", recommend="solid", reason=off, shell_ok=False, shell_off=off)))
    s = _state(live, sid)
    assert s["radios"] == [["auto", True, False], ["shell", False, True], ["solid", False, False]]
    assert s["auto"] == "Solid" and s["why"] == off
    assert_clean(live, "the FEA stage of a triangle file")


def test_auto_shell_and_solid_draws_the_mid_surface_and_what_stays_solid(live):
    sid = "feamixed"
    ox, oy, oz = (1000 * v for v in ORIGIN)
    mixed = _shell("ready", recommend="mixed",
                   reason="Thin walls meshed as shell (63% of the part, 8 mm thick); 1 thick region kept solid and "
                          "joined to them.",
                   share=0.63, t_min_mm=8.0, t_max_mm=12.0, bodies=[{"body": "Part_1", "verdict": "mixed"}],
                   not_shelled=[{"id": "S1", "kind": "solid", "words": "A thick region of the part (4 faces) stays "
                                 "solid: it is 30 mm thick or more, too thick for a shell.",
                                 "at_mm": [ox + 50, oy + 1000 * R_OUT, oz], "faces": []}],
                   open_edges=0, open_at_mm=[], overlay=True, legend_mm=[8.0, 12.0], scale=1.0)
    _open(live, sid, _check(mixed))
    live.wait_for(f"window._vdbg['gstage:{sid}'].shell().shown", timeout=30, what="the mid-surface to be drawn")
    s = _state(live, sid)
    assert s["radios"] == [["auto", True, False], ["shell", False, False], ["solid", False, False]]
    assert s["auto"] == "Shell + solid" and s["why"].startswith("Thin walls meshed as shell (63% of the part")
    assert s["rows"] == ["S1"] and s["list"] and s["legend"] and s["head"] == "Kept solid, joined to the shell"
    # Shell says what a plain shell leaves out; Solid hides it all
    _pick(live, sid, "shell")
    s = _state(live, sid)
    assert s["shell"]["shown"] and s["head"] == "If it is meshed as a shell"
    _pick(live, sid, "solid")
    assert not _state(live, sid)["shell"]["shown"]
    _pick(live, sid, "auto")
    assert _state(live, sid)["shell"]["shown"]
    live.evaluate(f"document.querySelector('#gstage-{sid} .gc-proceed').click()")
    live.wait_for(f"window.__confirmed['{sid}']", timeout=20, what="the confirmation")
    assert live.evaluate(f"window.__confirmed['{sid}'].fea_mesh_as") == "auto"
    assert_clean(live, "the FEA stage's shell + solid")


def _after(live, sid: str) -> dict:
    return live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{sid}'), n = root.querySelector('.iv-shell-note');
      return {{plain: root.classList.contains('iv-plain'), note: n.hidden ? '' : n.textContent.replace(/\\s+/g, ' ').trim(),
               waits: window.__stages['{sid}'].awaitsShell(), shell: window._vdbg['gstage:{sid}'].shell()}}; }})()""")


def test_the_mid_surface_lands_in_the_same_viewer_after_proceed(live):
    # the user asked for a shell and pressed Proceed in seconds; the shell check lands minutes later, while they are
    # on the mesh plan: the part is still in the intake's one viewer, and the surface is drawn there, with its legend
    sid = "feaafter"
    said = _shell("measuring", default="shell", said="shell", said_words="You asked for a shell mesh in the chat.")
    _open(live, sid, _check(said))
    live.evaluate(f"document.querySelector('#gstage-{sid} .gc-proceed').click()")
    live.wait_for(f"window.__confirmed['{sid}']", timeout=20, what="the confirmation")
    live.wait_for(f"document.getElementById('gstage-{sid}').classList.contains('iv-plain')", timeout=10,
                  what="the panel to go")
    settled = {"fea_mesh_as": "shell"}
    live.evaluate(f"window.__stages['{sid}'].update({json.dumps(dict(_check(said), confirmed=settled))})")
    a = _after(live, sid)
    assert a["plain"] and a["note"] == "Making the mid-surface…" and a["waits"] and not a["shell"]["shown"]
    live.evaluate(f"window.__stages['{sid}'].update({json.dumps(dict(_check(_ready()), confirmed=settled))})")
    live.wait_for(f"window._vdbg['gstage:{sid}'].shell().shown", timeout=30, what="the mid-surface after Proceed")
    a = _after(live, sid)
    assert a["shell"]["actors"] == 2 and not a["waits"]
    assert a["note"] == "Wall 8 mm – 12 mm"
    assert_clean(live, "the mid-surface after Proceed")


def test_the_watch_follows_the_shell_check_after_the_check_is_ready(live):
    # dev, 7 Nov: the check was ready (FEA naming done) while the shell check still measured; it landed on the worker
    # 10 s later, but the watch had stopped at "ready" and the panel said "measuring the walls…" for good. The watch
    # now goes on, quietly, until the shell view lands - the panel and the mid-surface follow with no reload
    sid = "feawatch"
    live.evaluate("""(async () => {
      const SKIN = __SKIN__, MEASURING = __MEASURING__, READY = __READY__, OVERLAY = __OVERLAY__;
      const real = window.fetch.bind(window);
      const json = (x) => Promise.resolve(new Response(JSON.stringify(x), {status: 200, headers: {'Content-Type': 'application/json'}}));
      window.__polls = 0; window.__landAt = 1e9;
      window.fetch = (u, o) => {
        const s = String(u);
        if (s.includes('/geometry/__S__/check/skin')) return json(SKIN);
        if (s.includes('/geometry/__S__/check/shell-surface')) return json(OVERLAY);
        if (s.endsWith('/geometry/__S__/check')) { window.__polls++; return json(window.__polls >= window.__landAt ? READY : MEASURING); }
        return real(u, o);
      };
      const { openGeometryStage } = await import('/static/js/viewer/geometry_stage.js');
      const c = await import('/static/js/shell/composer.js');
      const st = await import('/static/js/core/state.js');
      window.__stages = window.__stages || {};
      c.configureComposer({
        notice: {show() {}, clear() {}}, chat() {}, checkProgress() {}, geometryCard() {},
        geometryCheck: async (sid, d, confirm) => (window.__stages[sid] = await openGeometryStage(sid, d, confirm,
          {anchorEl: document.getElementById('stage'), fallback: () => {}})),
      });
      st.set.sessionId('__S__');
      window.__watch = c.watchGeometryCheck('__S__').then(() => { window.__watchDone = true; });
    })()""".replace("__SKIN__", json.dumps(_skin(_tube(R_OUT, R_IN))))
                  .replace("__MEASURING__", json.dumps(_check(_shell("measuring"))))
                  .replace("__READY__", json.dumps(_check(_ready())))
                  .replace("__OVERLAY__", json.dumps(_overlay())).replace("__S__", sid), timeout=60)
    live.wait_for(f"window._vdbg && window._vdbg['gstage:{sid}']", timeout=90, what="the FEA stage to initialise")
    s = _state(live, sid)
    assert s["auto"] == "measuring the walls…" and s["measuring"] and not s["shell"]["shown"]
    # the check is ready and the watch still runs: the shell check lands two polls on
    live.wait_for("window.__polls >= 2", timeout=20, what="the watch to keep polling past ready")
    assert not live.evaluate("!!window.__watchDone")
    live.evaluate("window.__landAt = window.__polls + 1")
    live.wait_for(f"window._vdbg['gstage:{sid}'].shell().shown", timeout=30, what="the mid-surface without a reload")
    s = _state(live, sid)
    assert s["auto"] == "Shell" and not s["measuring"] and s["legend"] and s["legendText"] == "8 mm – 12 mm"
    # drawn: the watch stops by itself
    live.wait_for("window.__watchDone === true", timeout=20, what="the watch to stop once the surface is drawn")
    assert_clean(live, "the watch following the shell check")


def test_auto_settled_solid_while_measuring_draws_no_mid_surface_after_proceed(live):
    # Proceed while the walls were measured, on Auto: the run is a solid (said on the stage), so the surface that
    # lands later is not drawn and nothing waits for it
    sid = "feaaftersolid"
    _open(live, sid, _check(_shell("measuring")))
    live.evaluate(f"document.querySelector('#gstage-{sid} .gc-proceed').click()")
    live.wait_for(f"window.__confirmed['{sid}']", timeout=20, what="the confirmation")
    settled = {"fea_mesh_as": "solid"}
    live.evaluate(f"window.__stages['{sid}'].update({json.dumps(dict(_check(_shell('measuring')), confirmed=settled))})")
    a = _after(live, sid)
    assert a["note"] == "" and not a["waits"]
    live.evaluate(f"window.__stages['{sid}'].update({json.dumps(dict(_check(_ready()), confirmed=settled))})")
    a = _after(live, sid)
    assert a["note"] == "" and not a["waits"] and not a["shell"]["shown"]
    assert_clean(live, "a solid settled while measuring")
