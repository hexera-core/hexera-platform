# Responsibility: Verify the plan step is a conversation (NOTE-18, NOTE-19): one input (the chat box); a requirement
#   typed with a picked face shows the user's bubble with its chip, a quiet "Reading…", the reply; the list of what will
#   be applied and the one Proceed stay in reach in a slim bar pinned above the chat box (a count that opens the list,
#   Proceed at its right end, the narrow panel too) and not in the card; a reply whose reading takes longer than the
#   send waits arrives with the card's poll and takes the place of "Reading…"; "I'm good to mesh" starts the run; over
#   the file limit the box says so before anything is sent.
# Boundaries: the page's own wiring (ui/js/main.js); only the network and the run's start are stood in for. It reads
#   the DOM; with HEXERA_SHOTS set it also saves screenshots there for a person to look at.
from __future__ import annotations

import base64
import json
import os

import pytest
from conftest import assert_clean

pytestmark = pytest.mark.ui

FACE = {"id": "Face_32", "label": "Face 32 · round Ø6 mm"}
PLAN_LINE = "Here's the plan. Tell me any mesh requirements here, then say go or press Proceed."
BRIEF = {"purpose": "structural", "purpose_label": "Structural (FEA)", "engine": "gmsh", "engine_label": "Gmsh",
         "label": "Bracket static stress", "user_notes": [], "proceed_ask": PLAN_LINE,
         "checks": [{"label": "Closed solid", "status": "pass"}, {"label": "Units: mm", "status": "pass"}]}
ROW_FACE = {"phrase": 0, "quote": "can you do a 1 mm mesh sizing in here?", "words": "can you do a 1 mm mesh sizing in here?",
            "size": "1 mm", "parts": [], "picked": True, "faces": [FACE]}
ROW_PIC = {"phrase": 1, "quote": "in that crevice there I want a very refined mesh",
           "words": "in that crevice there I want a very refined mesh", "size": "finer", "parts": ["Feature_3"],
           "parts_text": "the fillet (radius 8 mm)"}
ROW_RAIL = {"phrase": 2, "quote": "2 mm on the rail", "words": "2 mm on the rail", "size": "2 mm", "parts": ["Part_2"],
            "parts_text": "part 2"}
REPLY_FACE = "All right, 1 mm on Face 32 (round Ø6 mm)."
REPLY_PIC = ("Got it, finer mesh at the fillet. That's my guess from your words, so click the model if you meant "
             "somewhere else. A number only in the picture isn't used.")
REPLY_RAIL = "Sounds good, 2 mm on part 2."

SETUP = """
  const S = await import('/static/js/core/state.js');
  const C = await import('/static/js/shell/composer.js');
  const A = await import('/static/js/shell/attach.js');
  const chips = await import('/static/js/shell/chips.js');
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const png = await new Promise(r => { const c = document.createElement('canvas'); c.width = 80; c.height = 60;
    const g = c.getContext('2d'); g.fillStyle = '#888'; g.fillRect(0, 0, 80, 60); g.fillStyle = '#c33';
    g.fillRect(30, 20, 20, 14); c.toBlob(r, 'image/png'); });
  const pic = (name) => new File([png], name || 'image.png', {type: 'image/png'});
  const BRIEF = __BRIEF__, ROWS = [];
  const done = () => ({status: 'done', rows: ROWS.slice(), unsure: []});
  const brief = (pm) => ({...BRIEF, part_matches: pm || (ROWS.length ? done() : null)});
  window.__calls = []; window.__started = []; window.__parts = []; window.__sent = [];
  let n = 0;
  const real = window.fetch;
  const json = (x) => new Response(JSON.stringify(x), {status: 200, headers: {'Content-Type': 'application/json'}});
  window.fetch = async (url, opts = {}) => {
    const u = String(url);
    if (u.startsWith('/api/v1/chat/attachments/'))
      return new Response(png, {status: 200, headers: {'Content-Type': 'image/png'}});
    if (u === '/api/v1/chat/attachments') {
      const f = opts.body.get('file'); n += 1; await sleep(40);
      return json({id: ('a' + n).padEnd(16, '0'), kind: 'image', name: f.name, mime: f.type, bytes: f.size});
    }
    if (u === '/api/v1/chat/message') {
      const body = JSON.parse(opts.body); window.__calls.push(body); const t0 = performance.now();
      const t = body.content;
      const answer = (x) => { window.__sent.push(performance.now() - t0); return json(x); };
      if (t === 'static stress') return answer({session_id: 's-conv', reply: BRIEF.proceed_ask, brief: brief()});
      if (/good to mesh/.test(t)) return answer({session_id: 's-conv', reply: 'queued', done: true, job_id: 'job-1'});
      // A READING THAT TAKES LONGER THAN THE SEND WAITS: the send answers at once, the reply comes with the poll
      if (/rail/.test(t)) {
        window.__parts = [{status: 'reading'}, {...done(), rows: [...ROWS, __ROW_RAIL__], reply: __REPLY_RAIL__}];
        return answer({session_id: 's-conv', reply: '', reply_later: true, awaiting_confirmation: true,
                       brief: brief({...done(), status: 'reading'})});
      }
      await sleep(400);                                   // the reading: the bubble and "Reading…" show meanwhile
      if (/1 mm/.test(t)) { ROWS.push(__ROW_FACE__); return answer({session_id: 's-conv', reply: __REPLY_FACE__, brief: brief(), awaiting_confirmation: true}); }
      if (/crevice/.test(t)) { ROWS.push(__ROW_PIC__); return answer({session_id: 's-conv', reply: __REPLY_PIC__, brief: brief(), awaiting_confirmation: true}); }
      return answer({session_id: 's-conv', reply: 'ok', brief: brief()});
    }
    if (u.includes('/check/parts')) {
      const next = window.__parts.length > 1 ? window.__parts.shift() : window.__parts[0];
      if (next && next.rows) { ROWS.splice(0, ROWS.length, ...next.rows); }
      return json(next || {status: 'none'});
    }
    if (u.startsWith('/api/')) return json({});
    return real(url, opts);
  };
  C.configureComposer({onJobStarted: (id) => window.__started.push(id)});
  S.set.sessionId('s-conv');
  C.enableInput();
  const inp = document.getElementById('chat-input');
  const type = async (t) => { inp.value = t; inp.dispatchEvent(new Event('input'));
    inp.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true})); };
  const paste = (files) => { const dt = new DataTransfer(); files.forEach(f => dt.items.add(f));
    inp.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true})); };
  // THE PLAN BAR as drawn: its count, hint, Proceed, open list, rows and where it sits
  const bar = () => { const b = document.getElementById('plan-bar'); if (!b) return null;
    const vis = (e) => !!e && !e.hidden && e.getBoundingClientRect().height > 0;
    const count = b.querySelector('.pb-count'), go = b.querySelector('.rc-go'), list = b.querySelector('.pb-list');
    const r = b.getBoundingClientRect(), g = go.getBoundingClientRect(), box = document.getElementById('input-inner').getBoundingClientRect();
    return {count: vis(count) ? count.textContent.trim() : '', hint: b.querySelector('.pb-hint').textContent,
            go: vis(go) ? go.textContent : '', open: vis(list),
            rows: [...b.querySelectorAll('.rc-pm-item')].map(it => ({
              words: (it.querySelector('.rc-pm-q') || {}).textContent || '', size: (it.querySelector('.rc-pm-s') || {}).textContent || '',
              faces: [...it.querySelectorAll('.box-chip-face .box-chip-label')].map(c => c.textContent),
              parts: [...it.querySelectorAll('.rc-pm-chip')].map(c => c.dataset.part),
              xs: it.querySelectorAll('.rc-pm-x').length})),
            aboveBox: r.bottom <= box.top + 1, goRight: Math.round(box.right - g.right),
            goOnLine: Math.abs((g.top + g.bottom) / 2 - (count.hidden ? (g.top + g.bottom) / 2 : (count.getBoundingClientRect().top + count.getBoundingClientRect().bottom) / 2)) <= 2,
            inside: r.left >= -0.5 && r.right <= window.innerWidth + 0.5 && document.documentElement.scrollWidth <= window.innerWidth}; };
  window.__t = {C, A, chips, sleep, pic, type, paste, inp, bar};
  await C.say('static stress');
"""


def _setup() -> str:
    return (SETUP.replace("__BRIEF__", json.dumps(BRIEF)).replace("__ROW_FACE__", json.dumps(ROW_FACE))
            .replace("__ROW_PIC__", json.dumps(ROW_PIC)).replace("__ROW_RAIL__", json.dumps(ROW_RAIL))
            .replace("__REPLY_FACE__", json.dumps(REPLY_FACE)).replace("__REPLY_PIC__", json.dumps(REPLY_PIC))
            .replace("__REPLY_RAIL__", json.dumps(REPLY_RAIL)))


def _size(live, width: int, height: int) -> None:
    live._send("Emulation.setDeviceMetricsOverride",
               {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})
    live.evaluate("new Promise(r => setTimeout(r, 300))")


def _shot(live, name: str) -> None:
    out = os.environ.get("HEXERA_SHOTS")
    if not out:
        return
    data = live._send("Page.captureScreenshot", {"format": "png"})["data"]
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, name), "wb") as fh:
        fh.write(base64.b64decode(data))


def test_the_plan_step_has_one_input_and_the_plan_bar_with_proceed_above_the_chat_box(live):
    _size(live, 1280, 860)
    out = live.evaluate("(async () => {" + _setup() + """
      const card = document.querySelector('.req-card');
      return {notesBox: document.querySelectorAll('.rc-notes-in, .rc-notes-add, .rc-fin-btn, .req-card textarea').length,
              finalize: /Finalize notes|Add a note/.test(document.body.innerText),
              inCard: card.querySelectorAll('.rc-go, .rc-pm').length, heads: [...card.querySelectorAll('.rc-h')].map(h => h.textContent),
              chat: !document.getElementById('chat-input').disabled, bar: window.__t.bar(),
              plan: document.getElementById('cc').innerText.includes('say go or press Proceed')};
    })()""", timeout=60)
    assert out["notesBox"] == 0 and not out["finalize"], out
    assert out["chat"] and out["plan"], out
    # the card keeps the plan and the checks; the list and Proceed are not in it (no duplicates)
    assert out["inCard"] == 0 and out["heads"] == ["Mesh plan", "Checked"], out
    b = out["bar"]
    # with no requirement yet: a quiet hint and Proceed, at the right end, just above the chat box
    assert b["count"] == "" and b["hint"] == "Type any mesh requirement below, or press Proceed.", b
    assert b["go"] == "Proceed" and b["aboveBox"] and abs(b["goRight"]) <= 1 and b["inside"], b
    _shot(live, "01_plan_step_empty.png")
    assert_clean(live, "the plan step")


def test_requirements_are_answered_and_kept_in_reach_in_the_plan_bar_and_saying_go_starts_the_run(live):
    _size(live, 1280, 860)
    live.evaluate("(async () => {" + _setup() + "})()", timeout=60)
    # 1) a size with a face picked on the model
    first = live.evaluate("""(async () => {
      const {chips, sleep, type, bar} = window.__t;
      chips.add('chat', {kind: 'face', label: 'Face 32 · round Ø6 mm', value: {id: 'Face_32', label: 'Face 32 · round Ø6 mm'}});
      await type('can you do a 1 mm mesh sizing in here?');
      await sleep(60);
      const bubble = [...document.querySelectorAll('#cc .im.user')].pop();
      const during = {pending: (document.querySelector('#cc .im-pending .txt') || {}).textContent || '',
                      chip: (bubble.querySelector('.box-chip-face') || {}).textContent || '',
                      words: bubble.querySelector('.bub').textContent};
      await sleep(700);
      const reply = [...document.querySelectorAll('#cc .im.assistant .txt')].pop().textContent.replace(/\\u00a0/g, ' ');
      return {during, reply, bar: bar(), pendingLeft: document.querySelectorAll('.im-pending').length,
              sent: window.__calls[window.__calls.length - 1], chipsLeft: chips.items('chat').length};
    })()""", timeout=60)
    assert first["during"]["pending"] == "Reading…", first
    assert "Face 32" in first["during"]["chip"] and first["during"]["words"].startswith("can you do"), first
    assert first["reply"] == REPLY_FACE and first["pendingLeft"] == 0, first
    assert first["bar"]["count"] == "1 requirement" and not first["bar"]["open"] and first["bar"]["go"] == "Proceed", first
    assert first["sent"]["faces"] == [FACE] and first["chipsLeft"] == 0, first
    # 2) a picture pasted, with words
    second = live.evaluate("""(async () => {
      const {A, sleep, type, paste, pic, bar} = window.__t;
      paste([pic('crevice.png')]);
      await A.idle('chat');
      await type('in that crevice there I want a very refined mesh');
      await sleep(900);
      const bubble = [...document.querySelectorAll('#cc .im.user')].pop();
      return {img: !!bubble.querySelector('.att-img img'), sent: window.__calls[window.__calls.length - 1],
              reply: [...document.querySelectorAll('#cc .im.assistant .txt')].pop().textContent.replace(/\\u00a0/g, ' '),
              bar: bar()};
    })()""", timeout=60)
    assert second["img"] and len(second["sent"]["attachments"]) == 1, second
    assert second["reply"] == REPLY_PIC and second["bar"]["count"] == "2 requirements", second
    # 3) A READING SLOWER THAN THE SEND WAITS: the send answers at once, "Reading…" stays, and the reply arrives with
    #    the card's poll of the reading, in the place "Reading…" held
    third = live.evaluate("""(async () => {
      const {sleep, type, bar} = window.__t;
      const before = document.querySelectorAll('#cc .im.assistant').length;
      await type('2 mm on the rail');
      await sleep(200);
      const held = {pending: document.querySelectorAll('#cc .im-pending').length,
                    replies: document.querySelectorAll('#cc .im.assistant').length - before, took: window.__sent[window.__sent.length - 1],
                    hint: bar().hint};
      for (let i = 0; i < 120 && document.querySelector('#cc .im-pending'); i++) await sleep(100);
      const last = document.querySelector('#cc').lastElementChild;
      return {held, pendingLeft: document.querySelectorAll('#cc .im-pending').length,
              reply: last.querySelector('.txt').textContent.replace(/\\u00a0/g, ' '),
              after: [...document.querySelectorAll('#cc .im')].slice(-2).map(e => e.classList.contains('user') ? 'user' : 'assistant'),
              bar: bar()};
    })()""", timeout=60)
    h = third["held"]
    assert h["pending"] == 1 and h["took"] < 50 and h["hint"] == "Reading…", third   # the bubble, "Reading…": no long wait
    assert third["pendingLeft"] == 0 and third["reply"] == REPLY_RAIL and third["after"] == ["user", "assistant"], third
    assert third["bar"]["count"] == "3 requirements", third
    # THE LIST OPENS IN PLACE: each row's words, size, and its face or part chips (parts with their x)
    opened = live.evaluate("(() => { document.querySelector('#plan-bar .pb-count').click(); return window.__t.bar(); })()")
    assert opened["open"] and [r["size"] for r in opened["rows"]] == ["1 mm", "finer", "2 mm"], opened
    assert opened["rows"][0]["faces"] == ["Face 32 · round Ø6 mm"], opened
    assert opened["rows"][1]["parts"] == ["Feature_3"] and opened["rows"][1]["xs"] == 1, opened
    assert opened["rows"][2]["parts"] == ["Part_2"] and opened["rows"][2]["xs"] == 1, opened
    assert opened["aboveBox"] and abs(opened["goRight"]) <= 1 and opened["goOnLine"] and opened["inside"], opened
    _shot(live, "02_three_requirements_list_open.png")
    closed = live.evaluate("(() => { document.querySelector('#plan-bar .pb-count').click(); return window.__t.bar(); })()")
    assert not closed["open"] and closed["count"] == "3 requirements", closed
    _shot(live, "03_list_closed.png")
    # THE NARROW PANEL: the bar fits, Proceed at its right end, the list opens inside it
    _size(live, 400, 820)
    narrow = live.evaluate("(() => { document.querySelector('#plan-bar .pb-count').click(); return window.__t.bar(); })()")
    assert narrow["open"] and narrow["inside"] and abs(narrow["goRight"]) <= 1 and narrow["goOnLine"], narrow
    _shot(live, "04_narrow_list_open.png")
    _size(live, 1280, 860)
    # 4) saying so starts the mesh, the same as the button; the bar goes with the plan step
    go = live.evaluate("""(async () => {
      const {sleep, type} = window.__t;
      await type("all right, I'm good to mesh");
      await sleep(200);
      return {started: window.__started, said: window.__calls[window.__calls.length - 1].content};
    })()""", timeout=60)
    assert go == {"started": ["job-1"], "said": "all right, I'm good to mesh"}, go


def test_over_the_file_limit_the_box_says_it_in_words_and_sends_nothing(live):
    out = live.evaluate("(async () => {" + _setup() + """
      const T = window.__t;
      const before = window.__calls.length;
      T.paste([1, 2, 3, 4, 5, 6].map(k => T.pic('p' + k + '.png')));
      await T.A.idle("chat"); await T.sleep(50);
      return {line: document.querySelector('#input-bar > .box-msg').textContent,
              chips: document.querySelectorAll('.box-chips[data-box="chat"] .box-chip').length,
              sent: window.__calls.length - before};
    })()""", timeout=60)
    assert out["line"] == "Only 5 files per message." and out["chips"] == 5 and out["sent"] == 0, out
