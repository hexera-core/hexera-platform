# Responsibility: Verify the parts checklist under the notes on the request card (NOTE-4/5 before approval): nothing is read until Finalize notes, which shows "Reading your notes…", then the rows and the approval question again; the model is turned in 3D (the picture only as the fallback) with the rows' numbers on their parts; "Reading your notes…" until the reading lands (polled), then one row per phrase with its parts and size and a line per phrase that could not be placed; a row opens the part list, a click on a part keeps it at once (no save button), and nothing at all shows when the notes name no part.
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
      const save = document.querySelectorAll('.rc-pm button:not(.rc-pm-row):not(.rc-pm-part)').length;
      document.querySelectorAll('.rc-pm-row')[1].click();
      const chips = [...document.querySelectorAll('.rc-pm-part')].map(c => c.textContent.trim());
      document.querySelector('.rc-pm-part[data-part="Part_2"]').click();
      await new Promise(r => setTimeout(r, 700));
      return {wait, rows, save, chips, picks, after: [...document.querySelectorAll('.rc-pm-row')].map(r => r.textContent.trim())};
    })()""".replace("__VIEW__", VIEW), timeout=40)
    assert out["wait"] == "Reading your notes…"
    assert out["rows"][0].startswith("3-4 mm for the frame rail") and "part 3" in out["rows"][0] and "3–4 mm" in out["rows"][0]
    assert "Couldn’t tell which part is “Refine the bracket”" in out["rows"][1] and "Pick parts" in out["rows"][1]
    assert out["save"] == 0, "no save or confirm button: the normal Approve takes the pick"
    assert out["chips"] == ["1Bolt", "2", "3Rail"]
    assert out["picks"] == [[1, ["Part_2"]]]
    assert any("part 2" in r and "finer" in r for r in out["after"])


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


def test_the_model_turns_in_3d_and_a_row_lights_its_parts(live):
    out = live.evaluate("""(async () => {
      const { Stage } = await import('/static/js/render/stage.js');
      const { mountPartsView } = await import('/static/js/viewer/parts_view.js');
      Stage.mount();
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
      let view = null;
      const parts = {get: async () => done, pick: async () => done, model: async () => model,
                     mount: async (h, m, o) => (view = await mountPartsView(h, m, o))};
      Stage.brief({purpose: 'structural', engine: 'gmsh', user_notes: [{text: 'the rail'}], part_matches: done},
                  async () => {}, parts);
      for (let i = 0; i < 100 && !(view && view.debug.ready()); i++) await new Promise(r => setTimeout(r, 100));
      return {canvas: !!document.querySelector('.rc-pm-fig canvas'), pic: !!document.querySelector('.rc-pm-pic'),
              ready: !!(view && view.debug.ready()), tris: view && view.debug.triangles,
              turn: !!document.querySelector('.rc-pm-flip'), tags: view ? view.debug.tags() : null};
    })()""", timeout=40)
    assert out["canvas"] and not out["pic"], "the user turns the model in 3D, not a picture"
    assert out["ready"] and out["tris"] == 24 and not out["turn"]
    assert out["tags"] == ["1"], "the row's part carries its number; the grey part none"


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
