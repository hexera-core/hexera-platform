# Responsibility: Verify the parts checklist under the notes on the request card (NOTE-4 before approval): "Reading your notes…" until the reading lands (polled), then one row per phrase with its parts and size and a line per phrase that could not be placed; a row opens the part list, a click on a part keeps it at once (no save button), and nothing at all shows when the notes name no part.
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
