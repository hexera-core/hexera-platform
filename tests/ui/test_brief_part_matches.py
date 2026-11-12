# Responsibility: Verify the parts checklist under the notes on the request card (NOTE-4/5 before approval): nothing is read until Finalize notes, which shows "Reading your notes…", then the rows and the approval question again; the model is drawn on the intake's ONE viewer beside the conversation (never a second one in the card; the picture in the card only as the fallback) with the rows' numbers on their parts; "Reading your notes…" until the reading lands (polled), then one row per size request with its parts as chips and a row per phrase that could not be placed; a row is made active, a click on the model then puts a part in or takes it out (the chip follows at once, kept at once, no save button), ✕ takes a part out, the viewer goes when the run starts, and nothing at all shows when the notes name no part.
# Boundaries: the console's own modules in the page; the network is a stub object handed to the card. It reads the DOM, never pixels.
from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

VIEW = """{status:'done', picture:'', parts:[{id:'Part_1', n:'1', name:'Bolt', colour:'#4E79A7'},
  {id:'Part_2', n:'2', name:'', colour:'#F28E2B'}, {id:'Part_3', n:'3', name:'Rail', colour:'#59A14F'}],
  rows:[{phrase:0, words:'3-4 mm for the frame rail', quote:'3-4 mm for the frame rail', parts:['Part_3'],
         parts_text:'part 3', size:'3–4 mm'}],
  unsure:[{phrase:1, words:'Refine the bracket', quote:'Refine the bracket',
           text:'Couldn\\u2019t tell which part is “Refine the bracket”', size:'finer'}]}"""


def test_reading_then_rows_then_a_pick_kept_at_once(live):
    out = live.evaluate("""(async () => {
      const { Stage } = await import('/static/js/render/stage.js');
      Stage.mount();
      const done = __VIEW__;
      let gets = 0; const picks = [];
      const parts = {get: async () => (++gets < 2 ? {status: 'reading'} : done),
                     pick: async (i, ids) => { picks.push([i, ids]);
                       return {...done, rows: [...done.rows, {phrase: i, words: 'Refine the bracket', quote: '',
                               parts: ids, parts_text: 'part 2', size: 'finer'}], unsure: []}; }};
      Stage.brief({purpose:'structural', engine:'gmsh', user_notes:[{text:'3-4 mm for the frame rail'}],
                   part_matches:{status:'reading'}}, async () => {}, parts);
      const wait = document.querySelector('.rc-pm-wait').textContent;
      await new Promise(r => setTimeout(r, 6500));
      const rows = [...document.querySelectorAll('.rc-pm-row')].map(r => r.textContent.trim());
      const chips = [...document.querySelectorAll('.rc-pm-chip')].map(c => c.textContent.trim());
      const save = document.querySelectorAll('.rc-pm button:not(.rc-pm-x)').length;
      // the unsure phrase made active, then a part of the model clicked: its chip is there at once
      document.querySelectorAll('.rc-pm-row')[1].click();
      const active = document.querySelector('.rc-pm-item.active').dataset.phrase;
      Stage._pmClick('Part_2');
      const now = [...document.querySelectorAll('.rc-pm-item[data-phrase="1"] .rc-pm-chip')].map(c => c.textContent.trim());
      await new Promise(r => setTimeout(r, 700));
      return {wait, rows, chips, save, active, now, picks,
              after: [...document.querySelectorAll('.rc-pm-item')].map(r => r.textContent.trim())};
    })()""".replace("__VIEW__", VIEW), timeout=40)
    assert out["wait"] == "Reading your notes…"
    assert out["rows"][0] == "3-4 mm for the frame rail3–4 mm", out["rows"]
    assert out["rows"][1] == "Couldn’t tell which part is “Refine the bracket”finer", out["rows"]
    assert out["chips"] == ["Part 3 · Rail✕"], "a row's parts are chips under it"
    assert out["save"] == 0, "no save or confirm button: the yes takes the pick"
    assert out["active"] == "1"
    assert out["now"] == ["Part 2✕"], "the chip follows the click at once, before the server answers"
    assert out["picks"] == [[1, ["Part_2"]]]
    assert any("Refine the bracket" in r and "Part 2" in r and "finer" in r for r in out["after"])


def test_the_x_on_a_chip_takes_the_part_out(live):
    out = live.evaluate("""(async () => {
      const { Stage } = await import('/static/js/render/stage.js');
      Stage.mount();
      const done = {status: 'done', order: ['Part_1', 'Part_2'],
        parts: [{id: 'Part_1', n: '1', name: 'Bolt'}, {id: 'Part_2', n: '2', name: ''}],
        rows: [{phrase: 0, words: 'chassis', quote: 'chassis 3-4 mm', parts: ['Part_1', 'Part_2'], size: '3–4 mm'}], unsure: []};
      const picks = [];
      const parts = {get: async () => done, pick: async (i, ids) => { picks.push([i, ids]); return null; }};
      Stage.brief({purpose:'structural', engine:'gmsh', user_notes:[{text:'chassis 3-4 mm'}], part_matches: done},
                  async () => {}, parts);
      const before = [...document.querySelectorAll('.rc-pm-chip')].map(c => c.textContent.trim());
      document.querySelector('.rc-pm-x[data-part="Part_1"]').click();
      const after = [...document.querySelectorAll('.rc-pm-chip')].map(c => c.textContent.trim());
      await new Promise(r => setTimeout(r, 700));
      return {before, after, picks, active: !!document.querySelector('.rc-pm-item.active')};
    })()""")
    assert out["before"] == ["Part 1 · Bolt✕", "Part 2✕"]
    assert out["after"] == ["Part 2✕"]
    assert out["picks"] == [[0, ["Part_2"]]]
    assert out["active"] is False, "✕ takes the part out without making the row active"


def test_no_parts_in_the_notes_shows_nothing(live):
    out = live.evaluate("""(async () => {
      const { Stage } = await import('/static/js/render/stage.js');
      Stage.mount();
      Stage.brief({purpose:'structural', engine:'gmsh', user_notes:[{text:'static stress'}]}, async () => {});
      return document.querySelector('.rc-pm').children.length + document.querySelectorAll('.rc-pm-row').length;
    })()""")
    assert out == 0


def test_finalize_notes_reads_then_shows_the_rows_and_asks_to_proceed(live):
    out = live.evaluate("""(async () => {
      const { Stage } = await import('/static/js/render/stage.js');
      Stage.mount();
      const done = __VIEW__;
      let fin = 0;
      const parts = {get: async () => (fin ? done : {status: 'ready'}),
                     finalize: async () => { fin++; return {status: 'reading'}; }, pick: async () => done};
      Stage.brief({purpose:'structural', engine:'gmsh', user_notes:[{text:'3-4 mm for the frame rail'}],
                   proceed_ask: 'Shall I proceed with mesh generation?', part_matches:{status:'ready'}},
                  async () => {}, parts);
      const before = {btn: [...document.querySelectorAll('.rc-fin-btn')].map(b => b.textContent),
                      rows: document.querySelectorAll('.rc-pm-row').length};
      document.querySelector('.rc-fin-btn').click();
      await new Promise(r => setTimeout(r, 100));
      const wait = (document.querySelector('.rc-pm-wait') || {}).textContent;
      await new Promise(r => setTimeout(r, 3200));
      const lines = [...document.querySelectorAll('#cc .im .txt')].map(t => t.textContent.trim());
      return {before, fin, wait, rows: document.querySelectorAll('.rc-pm-row').length,
              btn: document.querySelectorAll('.rc-fin-btn').length, lines};
    })()""".replace("__VIEW__", VIEW), timeout=30)
    assert out["before"] == {"btn": ["Finalize notes"], "rows": 0}, "nothing is read until the user finalizes"
    assert out["fin"] == 1 and out["wait"] == "Reading your notes…"
    assert out["rows"] == 2 and out["btn"] == 0
    assert out["lines"][-1] == "Shall I proceed with mesh generation?", "the approval question under the rows"


MODEL = """
      // two boxes side by side, part 1 and part 2, as the server's parts model
      const P = [], T = [], part = [];
      const box = (x0, k) => { const b = P.length / 3;
        for (let i = 0; i < 8; i++) P.push(x0 + (i & 1), (i >> 1) & 1, (i >> 2) & 1);
        [[0,1,3],[0,3,2],[4,6,7],[4,7,5],[0,4,5],[0,5,1],[2,3,7],[2,7,6],[0,2,6],[0,6,4],[1,5,7],[1,7,3]]
          .forEach(t => { T.push(b + t[0], b + t[1], b + t[2]); part.push(k); }); };
      box(0, 1); box(2, 2);
      const b64 = (a) => btoa(String.fromCharCode(...new Uint8Array(a.buffer)));
      const model = {order: ['Part_1', 'Part_2'], points_b64: b64(Float32Array.from(P)),
        normals_b64: b64(new Float32Array(P.length)), tris_b64: b64(Uint32Array.from(T)),
        part_b64: b64(Uint16Array.from(part)), edges_b64: '', spots: []};
      const done = {status: 'done', order: ['Part_1', 'Part_2'], model: true,
        parts: [{id: 'Part_1', n: '1', name: ''}, {id: 'Part_2', n: '2', name: ''}],
        rows: [{phrase: 0, words: 'the rail', quote: 'the rail', parts: ['Part_1'], parts_text: 'part 1', size: '3 mm'}],
        unsure: []};
"""


def _stage_under(live):
    """The settled geometry stage the parts model is drawn over, as in the product (its own mouse is live under it)."""
    import json

    from test_geometry_stage import _check, _skin
    live.evaluate("""(async () => {
      const SKIN = __SKIN__;
      const real = window.fetch.bind(window);
      window.fetch = (u, o) => String(u).includes('/check/skin')
        ? Promise.resolve(new Response(JSON.stringify(SKIN), {status: 200, headers: {'Content-Type': 'application/json'}}))
        : real(u, o);
      const { openGeometryStage } = await import('/static/js/viewer/geometry_stage.js');
      const st = await openGeometryStage('pm-under', __CHECK__, async () => ({message: 'ok'}),
        {anchorEl: document.getElementById('stage')});
      st.settle();
    })()""".replace("__SKIN__", json.dumps(_skin())).replace("__CHECK__", json.dumps(_check())), timeout=60)
    live.wait_for("window._vdbg && window._vdbg['gstage:pm-under']", timeout=60, what="the geometry stage")


def _mount_on_the_viewer(live):
    live.evaluate("""(async () => {
      const { Stage } = await import('/static/js/render/stage.js');
      const { mountPartsView } = await import('/static/js/viewer/parts_view.js');
      const { partsHost, dropPartsHost } = await import('/static/js/viewer/intake_view.js');
      Stage.mount();
      __MODEL__
      window.__picks = []; window.__view = null;
      const parts = {get: async () => done, model: async () => model,
                     pick: async (i, ids) => { window.__picks.push([i, ids]); return null; },
                     mount: async (h, m, o) => (window.__view = await mountPartsView(h, m, o)),
                     host: () => partsHost(), drop: (el) => dropPartsHost(el)};
      Stage.brief({purpose: 'structural', engine: 'gmsh', user_notes: [{text: 'the rail'}], part_matches: done},
                  async () => {}, parts);
    })()""".replace("__MODEL__", MODEL), timeout=40)
    live.wait_for("window.__view && window.__view.debug.ready()", timeout=40, what="the parts model to be drawn")


def test_the_model_is_drawn_on_the_one_viewer_beside_the_conversation(live):
    _mount_on_the_viewer(live)
    out = live.evaluate("""(() => ({
      inCard: document.querySelectorAll('.req-card canvas, .req-card .rc-pm-fig').length,
      onViewer: document.querySelectorAll('#workbench .viewer .iv-parts canvas').length,
      viewers: document.querySelectorAll('#workbench .viewer').length,
      split: document.getElementById('app').classList.contains('intake'),
      divider: !!document.querySelector('#workbench .iv-split'),
      tris: window.__view.debug.triangles, tags: window.__view.debug.tags(),
      chatLeft: document.getElementById('stage').getBoundingClientRect().right
                <= document.getElementById('workbench').getBoundingClientRect().left + 1}))()""")
    assert out["inCard"] == 0, "no second viewer in the chat card"
    assert out["onViewer"] == 1 and out["viewers"] == 1, out
    assert out["split"] and out["divider"] and out["chatLeft"], out
    assert out["tris"] == 24 and out["tags"] == ["1"], "the row's part carries its number; the grey part none"


# where on the page a part is seen: the middle of its pixels in the viewer's id pass
SEEN_AT = """(() => {
  const r = document.querySelector('.iv-parts').getBoundingClientRect(), v = window.__view.debug;
  let sx = 0, sy = 0, n = 0;
  for (let y = r.top + 2; y < r.bottom - 2; y += 6) for (let x = r.left + 2; x < r.right - 2; x += 6)
    if (v.slotAt(x, y) === __SLOT__) { sx += x; sy += y; n++; }
  return n ? [sx / n, sy / n] : null;
})()"""


def test_an_active_row_takes_a_part_clicked_on_the_model(live):
    # the parts are drawn on the stage's own viewer: the stage under them must not take the clicks
    _stage_under(live)
    _mount_on_the_viewer(live)
    assert live.evaluate("document.querySelectorAll('#workbench .viewer').length") == 1, "one viewer"
    assert live.evaluate("!!document.querySelector('#gstage-pm-under > .iv-parts')"), "the parts on the stage's viewer"
    live.evaluate("document.querySelector('.rc-pm-row').click()")
    live.wait_for("document.querySelector('.rc-pm-item.active') && window.__view.debug.ready()", timeout=10)
    xy = live.evaluate(SEEN_AT.replace("__SLOT__", "2"))
    assert xy, "part 2 is seen on the model"
    live.click(xy[0], xy[1])
    live.wait_for("document.querySelectorAll('.rc-pm-chip').length === 2", timeout=10,
                  what="the clicked part's chip")
    chips = live.evaluate("[...document.querySelectorAll('.rc-pm-chip')].map(c => c.textContent.trim())")
    assert chips == ["Part 1✕", "Part 2✕"]
    live.wait_for("window.__picks.length === 1", timeout=10, what="the pick to be kept")
    assert live.evaluate("window.__picks") == [[0, ["Part_1", "Part_2"]]]
    # and clicked again, it goes back out
    live.wait_for("window.__view.debug.ready()", timeout=10)
    xy = live.evaluate(SEEN_AT.replace("__SLOT__", "2"))
    live.click(xy[0], xy[1])
    live.wait_for("document.querySelectorAll('.rc-pm-chip').length === 1", timeout=10, what="the chip to go")


def test_the_viewer_goes_when_the_run_starts(live):
    _mount_on_the_viewer(live)
    out = live.evaluate("""(async () => {
      const { Stage } = await import('/static/js/render/stage.js');
      const { endIntake } = await import('/static/js/viewer/intake_view.js');
      // what attachJob does as the mesher starts
      Stage.mount(); endIntake();
      const app = document.getElementById('app');
      return {viewers: document.querySelectorAll('#workbench .viewer').length,
              parts: document.querySelectorAll('.iv-parts').length, wb: app.classList.contains('wb'),
              split: app.classList.contains('intake'), divider: !!document.querySelector('.iv-split')};
    })()""")
    assert out == {"viewers": 0, "parts": 0, "wb": False, "split": False, "divider": False}, out


def test_the_picture_tints_a_rows_parts_and_a_click_on_it_picks_a_part(live):
    out = live.evaluate("""(async () => {
      const { Stage } = await import('/static/js/render/stage.js');
      Stage.mount(); Stage._pmOpen = null;
      // a 20 x 10 model: the left half is part 1, the right half part 2 (the id picture), all grey (the shaded one)
      const png = async (fill) => { const c = new OffscreenCanvas(20, 10), g = c.getContext('2d');
        fill(g); return await c.convertToBlob({type: 'image/png'}); };
      const shaded = await png(g => { g.fillStyle = 'rgb(200,200,200)'; g.fillRect(0, 0, 20, 10); });
      const ids = await png(g => { g.fillStyle = 'rgb(1,0,200)'; g.fillRect(0, 0, 10, 10);
                                   g.fillStyle = 'rgb(2,0,200)'; g.fillRect(10, 0, 10, 10); });
      const done = {status: 'done', order: ['Part_1', 'Part_2'],
        parts: [{id: 'Part_1', n: '1', name: ''}, {id: 'Part_2', n: '2', name: ''}],
        rows: [{phrase: 0, words: 'the rail', quote: 'the rail', parts: ['Part_1'], parts_text: 'part 1', size: '3 mm'}],
        unsure: [], views: [{name: 'card_a', w: 20, h: 10, tags: [], hidden: []}]};
      const picks = [];
      const parts = {get: async () => done, pick: async (i, p) => { picks.push([i, p]); return done; },
                     picture: async (n) => (n === 'card_a' ? shaded : ids)};
      Stage.brief({purpose: 'structural', engine: 'gmsh', user_notes: [{text: 'the rail'}], part_matches: done},
                  async () => {}, parts);
      await new Promise(r => setTimeout(r, 400));
      const px = (x) => { const c = document.querySelector('.rc-pm-pic');
        return [...c.getContext('2d').getImageData(x, 5, 1, 1).data].slice(0, 3); };
      const tinted = px(2), grey = px(15);
      document.querySelector('.rc-pm-row').click();
      await new Promise(r => setTimeout(r, 400));
      const c = document.querySelector('.rc-pm-pic'), b = c.getBoundingClientRect();
      c.dispatchEvent(new MouseEvent('click', {bubbles: true, clientX: b.left + b.width * 0.75,
                                               clientY: b.top + b.height / 2}));
      await new Promise(r => setTimeout(r, 700));
      return {tinted, grey, picks};
    })()""", timeout=30)
    r, g, b = out["tinted"]
    assert not (r == g == b), "a row's part is drawn in the row's colour"
    assert out["grey"][0] == out["grey"][1] == out["grey"][2], "a part no row means stays grey"
    assert out["picks"] == [[0, ["Part_1", "Part_2"]]], "a click on the picture adds the part under it"


def test_a_flow_card_finalizes_its_notes_and_shows_how_they_were_read(live):
    """NOTES-CFD-1: a CFD request card with notes and no parts to name has the same Finalize notes; pressing it shows
    "Reading your notes…", then the CFD reader's lines, then the approval question under the card - as on FEA."""
    out = live.evaluate("""(async () => {
      const { Stage } = await import('/static/js/render/stage.js');
      Stage.mount();
      let fin = 0;
      const done = {status: 'done', rows: [], unsure: [],
                    lines: ['From your notes: 5 boundary layers on the wall.', 'From your notes: at most 2 million cells.']};
      const parts = {get: async () => (fin ? done : {status: 'ready'}),
                     finalize: async () => { fin++; return {status: 'reading'}; }, pick: async () => null};
      Stage.brief({purpose: 'internal flow', engine: 'snappy', user_notes: [{text: '5 layers, under 2M cells'}],
                   proceed_ask: 'Shall I proceed with mesh generation?', part_matches: {status: 'ready'}},
                  async () => {}, parts);
      const before = {btn: [...document.querySelectorAll('.rc-fin-btn')].map(b => b.textContent),
                      primary: document.querySelector('.rc-fin-btn').classList.contains('gc-proceed')};
      document.querySelector('.rc-fin-btn').click();
      await new Promise(r => setTimeout(r, 100));
      const wait = (document.querySelector('.rc-pm-wait') || {}).textContent;
      await new Promise(r => setTimeout(r, 3200));
      return {before, fin, wait, btn: document.querySelectorAll('.rc-fin-btn').length,
              lines: [...document.querySelectorAll('.rc-pm-lines li')].map(l => l.textContent),
              rows: document.querySelectorAll('.rc-pm-row').length,
              said: [...document.querySelectorAll('#cc .im .txt')].map(t => t.textContent.trim()).pop()};
    })()""", timeout=30)
    assert out["before"] == {"btn": ["Finalize notes"], "primary": True}, "the same button as on FEA"
    assert out["fin"] == 1 and out["wait"] == "Reading your notes…"
    assert out["btn"] == 0 and out["rows"] == 0
    assert out["lines"] == ["From your notes: 5 boundary layers on the wall.", "From your notes: at most 2 million cells."]
    assert out["said"] == "Shall I proceed with mesh generation?", "the approval question follows, as on FEA"
