# Responsibility: Verify the run reads as a chat, not a box (Areen 2026-11-01): no card border and nothing that scrolls on its own inside the conversation; the steps as one sequence of small rows in time order; what Hexera says as plain text between them; the review's thinking and tool calls folded into its one row; the pictures as small thumbnails that open large; the clock and Cancel on one slim line above the composer.
# Boundaries: the console's own modules in the page, fed through the one event dispatch; no network. It reads computed styles and the DOM, never pixels.
from __future__ import annotations

import base64
import json

import pytest
from conftest import assert_clean

pytestmark = pytest.mark.ui

# A one-pixel PNG: what a reviewer picture event carries. Twelve different renders of it (bytes after
# the end chunk are ignored by a browser), so none of them is taken for a repeat of another.
_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
SHOTS = json.dumps([base64.b64encode(_PNG + bytes([i]) * (i + 1)).decode() for i in range(12)])

PLAY = """
  const { Stage } = await import('/static/js/render/stage.js');
  const { dispatch } = await import('/static/js/core/events.js');
  Stage.mount();
  let cur = null;
  const ev = (e) => { cur = dispatch(e, Stage, cur); };
  Stage.ensureProc(); Stage.showCancel();
  ev({type:'stage', stage:'engine_select'});
  ev({type:'note', stage:'engine_select', text:'Mesh engine: Gmsh - the one you asked for', tone:'info'});
  ev({type:'stage', stage:'builder'});
  ev({type:'narrate', stage:'builder', kind:'say', text:'Meshing the solid with 10-node tetrahedra.'});
  ev({type:'plan', stage:'builder', summary:'base size 15.8 mm, 57 wall areas'});
  ev({type:'meshing', stage:'builder', engine:'gmsh', budget_s:3600});
  ev({type:'meshed', stage:'builder', cells:115941});
  ev({type:'narrate', stage:'builder', kind:'say', text:'Quality looks good: 116k elements, worst element 0.233.'});
  ev({type:'stage', stage:'executor'});
  for (let i = 0; i < 9; i++) ev({type:'check', stage:'executor', statement:'Check ' + i, ok:true});
  ev({type:'stage', stage:'reviewer'});
  ev({type:'reasoning', stage:'reviewer', id:'r1', phase:'started', content:''});
  const thinking = document.getElementById('tl').innerText;
  ev({type:'reasoning', stage:'reviewer', id:'r1', phase:'completed', duration_ms:1900, token_count:12,
      content:'Identifying the patches worth a closer look.'});
  for (let i = 0; i < 3; i++) {
    ev({type:'tool_call', stage:'reviewer', id:'t' + i, tool_name:'zoom_to_region', arguments:{screen_x:0.5}});
    ev({type:'tool_result', stage:'reviewer', tool_call_id:'t' + i, status:'success', duration_ms:400});
  }
  for (const image of __SHOTS__) ev({type:'screenshot', stage:'reviewer', image});
  const running = {
    bar: !!document.querySelector('#input-bar > #run-status'),
    barShown: getComputedStyle(document.getElementById('run-status')).display !== 'none',
    cancel: !document.getElementById('proc-cancel').hidden,
    inRun: !!document.querySelector('.proc #proc-cancel, .proc #proc-clock'),
  };
  ev({type:'verdict', stage:'reviewer', verdict:'PASS'});
""".replace("__SHOTS__", SHOTS)


def test_the_run_is_rows_in_the_chat_with_nothing_boxed_or_scrolling_on_its_own(live):
    out = live.evaluate("(async () => {" + PLAY + """
      Stage.final({pass:true, createdAt:'2026-11-01T10:00:00Z', endedAt:'2026-11-01T10:03:20Z', attempts:1});
      const proc = document.querySelector('.proc'), cs = getComputedStyle(proc);
      // every element of the run: none may scroll on its own or cap its height
      const boxed = [...proc.querySelectorAll('*')].filter(e => {
        const s = getComputedStyle(e);
        return ['auto', 'scroll'].includes(s.overflowY) || ['auto', 'scroll'].includes(s.overflowX)
          || (s.maxHeight !== 'none' && e.scrollHeight > e.clientHeight + 1);
      }).map(e => e.className || e.tagName);
      const order = [...document.getElementById('tl').children].map(e =>
        e.classList.contains('tl-say') ? 'say' : e.classList.contains('tl-node') ? e.querySelector('.nm').textContent : e.className);
      const review = [...document.querySelectorAll('#tl .tl-node')].find(n => n.querySelector('.nm').textContent === 'Review');
      const shots = review.querySelectorAll('.e-shot');
      const shotBox = shots[0].getBoundingClientRect();
      shots[0].click();
      const lightbox = document.getElementById('lb').classList.contains('open');
      return {
        border: cs.borderTopWidth, background: cs.backgroundColor, boxed, order,
        thinking: running_thinking,
        reviewRow: review.querySelector('.tl-name').innerText.replace(/\\s+/g, ' ').trim(),
        reviewOpen: review.classList.contains('open'),
        reviewText: review.querySelector('.tl-body').textContent,
        shots: shots.length, shotW: shotBox.width, shotsFolded: !review.querySelector('.tl-body .e-shot'),
        lightbox, running,
        barDone: getComputedStyle(document.getElementById('run-status')).display,
        result: document.querySelector('.tl-result .tl-name').innerText.replace(/\\s+/g, ' ').trim(),
      };
    })()""".replace("running_thinking", "thinking"), timeout=60)
    # no card: no border, no panel fill, and nothing that scrolls inside the conversation
    assert out["border"] == "0px" and out["background"] in ("rgba(0, 0, 0, 0)", "transparent"), out
    assert out["boxed"] == [], f"these scroll or clip on their own: {out['boxed']}"
    # ONE sequence in time order: each step once, what Hexera said between them
    assert out["order"] == ["Engine selection", "Mesh creation", "say", "say", "Mesh validation", "Review", "Result"], out
    # the review folds its thinking and its calls into one muted row; unfolded, plain words
    assert out["reviewRow"].startswith("Review passed · 3 tool calls"), out
    assert not out["reviewOpen"], "a finished step stays folded"
    assert "Identifying the patches" in out["reviewText"] and "Zoom to region" in out["reviewText"]
    for raw in ("tokens", "1.9s", "screen_x", "tool call"):
        assert raw not in out["reviewText"], raw
    assert out["thinking"].count("Thinking…") == 1, "a running thought says so while it runs"
    # the pictures: small thumbnails in sight with the review folded; a click opens one large
    assert out["shots"] == 12 and out["shotW"] <= 100 and out["shotsFolded"] and out["lightbox"], out
    # the clock and Cancel: one slim line above the composer while it runs, gone once it ends
    r = out["running"]
    assert r["bar"] and r["barShown"] and r["cancel"] and not r["inRun"], r
    assert out["barDone"] == "none"
    assert out["result"].startswith("Result PASS 1 attempt") and out["result"].endswith("3:20"), out
    assert_clean(live, "the run as a chat")


def test_a_step_unfolds_and_folds_from_its_row_by_keyboard_too(live):
    out = live.evaluate("""(async () => {
      const { Stage } = await import('/static/js/render/stage.js');
      Stage.mount(); Stage.ensureProc();
      Stage.check('executor', 'Every cell is valid', true);
      Stage.done('executor');
      const n = document.querySelector('#tl .tl-node'), head = n.querySelector('.tl-name');
      const folded = [n.classList.contains('open'), head.getAttribute('aria-expanded')];
      head.focus();
      head.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
      const opened = [n.classList.contains('open'), head.getAttribute('aria-expanded')];
      head.click();
      return {folded, opened, closed: [n.classList.contains('open'), head.getAttribute('aria-expanded')],
              row: head.innerText.replace(/\\s+/g, ' ').trim()};
    })()""")
    assert out["folded"] == [False, "false"] and out["opened"] == [True, "true"], out
    assert out["closed"] == [False, "false"], out
    assert out["row"].startswith("Mesh validation 1 check passed"), out


def test_the_notes_box_is_one_quiet_line_that_grows_and_enter_adds(live):
    out = live.evaluate("""(async () => {
      const { Stage } = await import('/static/js/render/stage.js');
      Stage.mount();
      const sent = [];
      const add = async (t) => { sent.push(t);
        Stage.brief({purpose:'structural', engine:'gmsh', user_notes:[{text:'Static stress'}, {text:t}]}, add); };
      Stage.brief({purpose:'structural', engine:'gmsh', user_notes:[{text:'Static stress'}]}, add);
      const box = () => document.querySelector('.rc-notes-in'), btn = () => document.querySelector('.rc-notes-add');
      const idle = {h: box().getBoundingClientRect().height, disabled: btn().disabled,
                    btnFont: getComputedStyle(btn()).fontFamily, btnBg: getComputedStyle(btn()).backgroundColor};
      box().focus(); box().value = 'Bolt holes matter';
      box().dispatchEvent(new Event('input'));
      await new Promise(r => setTimeout(r, 250));
      const focused = {h: box().getBoundingClientRect().height, disabled: btn().disabled};
      box().dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true}));
      await new Promise(r => setTimeout(r, 50));
      return {idle, focused, sent, notes: [...document.querySelectorAll('.rc-notes-list li.rc-note')].map(n => n.textContent)};
    })()""")
    assert out["idle"]["h"] <= 36 and out["idle"]["disabled"], out
    assert "Geist" in out["idle"]["btnFont"], "the Add button is the browser's own"
    assert out["focused"]["h"] >= 60 and not out["focused"]["disabled"], out
    assert out["sent"] == ["Bolt holes matter"]
    assert out["notes"] == ["Static stress", "Bolt holes matter"], out
