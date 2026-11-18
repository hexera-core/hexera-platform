# Responsibility: Verify on the FEA stage in a real browser that a shell appears ONLY when the user asked for one in
# words (SHSTAGE-2..5): a plain FEA part's stage has no shell row, no pick, no legend and no "Making the mid-surface"
# line - the stage from before shell existed; a said shell opens with one "Mesh as: Shell" row and a line while the
# mid-surface is made; when the shell check lands, the panel follows in place - what is not in the shell (S1) and
# where the plates do not meet (O1), each marked on the part, the legend in mm - and the mid-surface is drawn over
# the part coloured by its wall. A shell asked for later appears then; one that cannot be made says why, draws
# nothing. Proceed never sends a pick (the server settles from the words).
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


def _shell(state: str, **kw) -> dict:
    base = {"question": "Mesh as", "state": state, "said": "shell", "said_words": "You asked for a shell mesh.",
            "mesh_as": "shell", "label": "Shell: the mid-surface, meshed with plate elements", "reason": "",
            "measuring_words": "Making the mid-surface. This can take a few minutes; you can proceed now."
            if state == "measuring" else ""}
    base.update(kw)
    return base


def _ready() -> dict:
    ox, oy, oz = (1000 * v for v in ORIGIN)
    return _shell("ready", share=0.95, t_min_mm=8.0, t_max_mm=12.0, bodies=[{"body": "Part_1", "verdict": "shell"}],
                  not_shelled=[
                      {"id": "S1", "kind": "solid", "words": "A thick region of the part (4 faces) stays solid: it is "
                       "30 mm thick or more, too thick for a shell.", "short": "Thick region · 30 mm+ thick, 4 faces", "at_mm": [ox + 50, oy + 1000 * R_OUT, oz], "faces": []},
                      {"id": "", "kind": "open_head", "words": "The plates do not meet at 1 place: shown in red.",
                       "at_mm": None, "faces": []},
                      {"id": "O1", "kind": "open", "words": "Plates do not meet here (1 of 1).",
                       "at_mm": [ox + 150, oy + 45, oz], "faces": []}],
                  open_edges=1, open_at_mm=[[ox + 150, oy + 45, oz]], overlay=True, legend_mm=[8.0, 12.0], scale=1.0)


def _check(shell: dict | None) -> dict:
    return {"status": "ready", "named": True, "skin": True, "pictures": [], "proposal": {
        "part": "tube", "input_kind": "body-surface", "size_mm": [300.0, 100.0, 100.0], "unit": "mm",
        "unit_basis": "file", "notes": [], "openings": [], "faces": [], "holes": []},
        "fea": {"question": "Is this your part and are the units right?", "bodies": 1, "clean": True,
                "clean_summary": "watertight", "single_part_only": "", "can_proceed": True,
                "measure": {"measured": True, "basis": "cad", "thickness_min_mm": 8.0, "thickness_face": "Face_1",
                            "hot_spots": [], "counts": {}, "more": 0, "summary": "no hot spots", "notes": []},
                **({"shell": shell} if shell else {})}}


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
      // drawn in the panel - in the shell section (always shown) or its 'Not in the shell' fold
      const vis = (sel) => !!root.querySelector(sel);
      return {{radios: root.querySelectorAll('input[type="radio"][name="fea-mesh-as"]').length,
               row: (root.querySelector('.fea-meshas-v') || {{}}).textContent || '',
               measuring: vis('.fea-meshas-measuring'), why: (root.querySelector('.fea-meshas-why') || {{}}).textContent || '',
               rows: [...root.querySelectorAll('.fea-shell-row')].map((r) => r.dataset.spot),
               list: vis('.fea-shell-list'), legend: vis('.fea-shell-legend'),
               head: (root.querySelector('.fea-shell-list > .fea-fold-btn') || {{}}).textContent || '',
               legendText: (root.querySelector('.fea-shell-legend-v') || {{}}).textContent || '',
               pins: [...root.querySelectorAll('.fea-shell-pin')].map((p) => p.dataset.spot),
               tag: root.querySelector('.fea-tag').textContent,
               sum: (root.querySelector('.fea-sec-shell .fea-sec-sum') || {{}}).textContent || '',
               said: (root.querySelector('.fea-meshas-said') || {{title: ''}}).title,
               closed: !root.querySelector('[data-fold].open'),
               shown: !!root.querySelector('.fea-sec-shell.fea-sec-shown'),
               panel: root.querySelector('.fea-panel').textContent,
               shell: h.shell(), fetches: window.__overlayFetches}}; }})()""")


def _proceed(live, sid: str) -> dict:
    live.evaluate(f"document.querySelector('#gstage-{sid} .gc-proceed').click()")
    live.wait_for(f"window.__confirmed['{sid}']", timeout=20, what="the confirmation")
    return live.evaluate(f"window.__confirmed['{sid}']")


def test_plain_fea_has_no_shell_anywhere(live):
    """(a) "FEA" alone: no shell view from the server, so no shell row, pick, legend, line or drawing - and
    Proceed sends no shell choice."""
    sid = "feasolid"
    _open(live, sid, _check(None))
    s = _state(live, sid)
    assert s["radios"] == 0 and s["row"] == "" and not s["measuring"] and not s["legend"] and s["rows"] == []
    assert "shell" not in s["panel"].lower() and "auto" not in s["panel"].lower()
    assert "mid-surface" not in s["panel"].lower()
    assert s["tag"] == "FEA" and s["sum"] == ""
    assert s["shell"] == {"actors": 0, "shown": False} and s["fetches"] == 0
    body = _proceed(live, sid)
    assert "fea_mesh_as" not in body
    a = _after(live, sid)
    assert a["note"] == "" and not a["waits"] and not a["shell"]["shown"]
    assert_clean(live, "a plain FEA stage")


def test_a_said_shell_follows_the_check_and_draws_the_mid_surface(live):
    """(b) "FEA, shell meshing": one row saying Shell, a line while the mid-surface is made; when the check lands the
    panel follows in place and the mid-surface is drawn, coloured by its wall."""
    sid = "feameshas"
    _open(live, sid, _check(_shell("measuring")))
    s = _state(live, sid)
    assert s["radios"] == 0 and s["row"].startswith("Shell: the mid-surface, meshed with plate elements")
    assert s["said"] == "You asked for a shell mesh." and s["measuring"]
    # one section, always shown; its header says the mid-surface is being made, the title's tag says shell
    assert s["tag"] == "FEA · shell" and s["sum"].strip() == "making the mid-surface…" and s["shown"] and s["closed"]
    assert "auto" not in s["panel"].lower() and "recommend" not in s["panel"].lower()
    assert s["shell"] == {"actors": 0, "shown": False} and s["fetches"] == 0
    live.evaluate(f"window.__stages['{sid}'].update({json.dumps(_check(_ready()))})")
    live.wait_for(f"window._vdbg['gstage:{sid}'].shell().shown", timeout=30, what="the mid-surface to be drawn")
    s = _state(live, sid)
    assert not s["measuring"] and " ".join(s["head"].split()) == "Not in the shell 2"
    assert s["rows"] == ["S1", "O1"] and s["list"] and s["legend"] and s["legendText"] == "8 mm – 12 mm"
    assert s["sum"] == "1 region stays solid · 1 open" and s["closed"]
    # the mark of what stays solid, clicked on the part, opens its fold and lights its line
    lit = live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{sid}');
      root.querySelector('.fea-shell-pin[data-spot="S1"]').click();
      const row = root.querySelector('.fea-shell-row[data-spot="S1"]');
      return [!!row.closest('[hidden]'), row.classList.contains('sel'), row.textContent.trim()]; }})()""")
    assert lit == [False, True, "S1Thick region · 30 mm+ thick, 4 faces"]
    assert s["pins"] == ["S1", "O1"]
    assert s["shell"]["actors"] == 2 and s["fetches"] == 1           # the surface and the open edges
    # a later poll with the same view changes nothing
    live.evaluate(f"window.__stages['{sid}'].update({json.dumps(_check(_ready()))})")
    assert _state(live, sid)["fetches"] == 1
    assert "fea_mesh_as" not in _proceed(live, sid)
    assert_clean(live, "the FEA stage of a said shell")


def test_a_shell_asked_for_later_appears_then(live):
    """A shell said in a note after the stage opened: the row and the mid-surface appear when the view does."""
    sid = "fealater"
    _open(live, sid, _check(None))
    assert _state(live, sid)["row"] == ""
    live.evaluate(f"window.__stages['{sid}'].update({json.dumps(_check(_shell('measuring')))})")
    s = _state(live, sid)
    assert s["row"].startswith("Shell:") and s["measuring"]
    live.evaluate(f"window.__stages['{sid}'].update({json.dumps(_check(_ready()))})")
    live.wait_for(f"window._vdbg['gstage:{sid}'].shell().shown", timeout=30, what="the mid-surface to be drawn")
    assert _state(live, sid)["legend"]
    assert_clean(live, "a shell asked for later")


def test_a_shell_that_cannot_be_made_says_why_and_draws_nothing(live):
    """(d) A shell asked for on a chunky part: Solid, with the one-sentence reason; no list, legend or drawing."""
    sid = "feachunky"
    why = "You asked for a shell, but this part has no plate or thin wall to make one from, so it is meshed as a solid."
    _open(live, sid, _check(_shell("ready", mesh_as="solid", label="Solid", reason=why)))
    s = _state(live, sid)
    assert s["row"] == "Solid" and s["why"] == why and not s["measuring"] and not s["legend"] and s["rows"] == []
    assert s["tag"] == "FEA · solid" and s["sum"] == "not possible · meshed solid"
    assert not s["shell"]["shown"] and s["fetches"] == 0
    assert_clean(live, "a shell that cannot be made")


def _after(live, sid: str) -> dict:
    return live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{sid}'), n = root.querySelector('.iv-shell-note');
      return {{plain: root.classList.contains('iv-plain'), note: n.hidden ? '' : n.textContent.replace(/\\s+/g, ' ').trim(),
               waits: window.__stages['{sid}'].awaitsShell(), shell: window._vdbg['gstage:{sid}'].shell()}}; }})()""")


def test_the_mid_surface_lands_in_the_same_viewer_after_proceed(live):
    # the user asked for a shell and pressed Proceed in seconds; the shell check lands minutes later, while they are
    # on the mesh plan: the part is still in the intake's one viewer, and the surface is drawn there, with its legend
    sid = "feaafter"
    said = _shell("measuring")
    _open(live, sid, _check(said))
    _proceed(live, sid)
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
    assert s["measuring"] and not s["shell"]["shown"]
    # the check is ready and the watch still runs: the shell check lands two polls on
    live.wait_for("window.__polls >= 2", timeout=20, what="the watch to keep polling past ready")
    assert not live.evaluate("!!window.__watchDone")
    live.evaluate("window.__landAt = window.__polls + 1")
    live.wait_for(f"window._vdbg['gstage:{sid}'].shell().shown", timeout=30, what="the mid-surface without a reload")
    s = _state(live, sid)
    assert not s["measuring"] and s["legend"] and s["legendText"] == "8 mm – 12 mm"
    # drawn: the watch stops by itself
    live.wait_for("window.__watchDone === true", timeout=20, what="the watch to stop once the surface is drawn")
    assert_clean(live, "the watch following the shell check")
