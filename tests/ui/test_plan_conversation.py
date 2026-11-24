# Responsibility: Verify the plan step is a conversation (NOTE-18): one input (the chat box), a requirement typed with
#   a picked face shows the user's bubble with its chip, a quiet "Reading…", the reply and a checklist row; a pasted
#   picture shows in the bubble; "I'm good to mesh" typed starts the run; over the file limit the box says so before
#   anything is sent; the one Proceed sits alone, right-aligned on the card's content edge.
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
REPLY_FACE = "1 mm elements on Face 32 (round Ø6 mm)."
REPLY_PIC = ("Finer mesh at the fillet (radius 8 mm). Click the model if you meant somewhere else. "
             "A number that is only in the picture is not used: type it if it matters.")

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
  const brief = () => ({...BRIEF, part_matches: ROWS.length ? {status: 'done', rows: ROWS.slice(), unsure: []} : null});
  window.__calls = []; window.__started = [];
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
      const body = JSON.parse(opts.body); window.__calls.push(body);
      const t = body.content;
      if (t === 'static stress') return json({session_id: 's-conv', reply: BRIEF.proceed_ask, brief: brief()});
      if (/good to mesh/.test(t)) return json({session_id: 's-conv', reply: 'queued', done: true, job_id: 'job-1'});
      await sleep(400);                                   // the reading: the bubble and "Reading…" show meanwhile
      if (/1 mm/.test(t)) { ROWS.push(__ROW_FACE__); return json({session_id: 's-conv', reply: __REPLY_FACE__, brief: brief(), awaiting_confirmation: true}); }
      if (/crevice/.test(t)) { ROWS.push(__ROW_PIC__); return json({session_id: 's-conv', reply: __REPLY_PIC__, brief: brief(), awaiting_confirmation: true}); }
      return json({session_id: 's-conv', reply: 'ok', brief: brief()});
    }
    if (u.includes('/check/parts')) return json({status: 'none'});
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
  window.__t = {C, A, chips, sleep, pic, type, paste, inp};
  await C.say('static stress');
"""


def _setup() -> str:
    return (SETUP.replace("__BRIEF__", json.dumps(BRIEF)).replace("__ROW_FACE__", json.dumps(ROW_FACE))
            .replace("__ROW_PIC__", json.dumps(ROW_PIC)).replace("__REPLY_FACE__", json.dumps(REPLY_FACE))
            .replace("__REPLY_PIC__", json.dumps(REPLY_PIC)))


def _shot(live, name: str, width: int = 1280, height: int = 860) -> None:
    out = os.environ.get("HEXERA_SHOTS")
    if not out:
        return
    live._send("Emulation.setDeviceMetricsOverride",
               {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})
    live.evaluate("new Promise(r => setTimeout(r, 300))")
    data = live._send("Page.captureScreenshot", {"format": "png"})["data"]
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, name), "wb") as fh:
        fh.write(base64.b64decode(data))


def test_the_plan_step_has_one_input_and_proceed_alone_on_the_cards_content_edge(live):
    out = live.evaluate("(async () => {" + _setup() + """
      const card = document.querySelector('.req-card'), go = card.querySelector('.rc-go');
      const row = go.parentElement, c = card.getBoundingClientRect(), b = go.getBoundingClientRect();
      const firstRow = card.querySelector('.rc-row .rc-k').getBoundingClientRect();
      return {notesBox: document.querySelectorAll('.rc-notes-in, .rc-notes-add, .rc-fin-btn, .req-card textarea').length,
              finalize: /Finalize notes|Add a note/.test(document.body.innerText),
              chat: !document.getElementById('chat-input').disabled, label: go.textContent,
              alone: row.children.length, last: card.lastElementChild === row,
              rightGap: Math.round(c.right - b.right), leftGap: Math.round(firstRow.left - c.left),
              plan: document.getElementById('cc').innerText.includes('say go or press Proceed')};
    })()""", timeout=60)
    assert out["notesBox"] == 0 and not out["finalize"], out
    assert out["chat"] and out["plan"], out
    assert out["label"] == "Proceed" and out["alone"] == 1 and out["last"], out
    # right-aligned on the content edge: the same inset on the right as the rows' text has on the left
    assert abs(out["rightGap"] - out["leftGap"]) <= 2, out
    _shot(live, "01_plan_step_empty.png")
    assert_clean(live, "the plan step")


def test_requirements_typed_in_the_chat_are_answered_and_listed_and_saying_go_starts_the_run(live):
    live.evaluate("(async () => {" + _setup() + "})()", timeout=60)
    # 1) a size with a face picked on the model
    first = live.evaluate("""(async () => {
      const {chips, sleep, type} = window.__t;
      chips.add('chat', {kind: 'face', label: 'Face 32 · round Ø6 mm', value: {id: 'Face_32', label: 'Face 32 · round Ø6 mm'}});
      await type('can you do a 1 mm mesh sizing in here?');
      await sleep(60);
      const bubble = [...document.querySelectorAll('#cc .im.user')].pop();
      const during = {pending: (document.querySelector('#cc .im-pending .txt') || {}).textContent || '',
                      chip: (bubble.querySelector('.box-chip-face') || {}).textContent || '',
                      words: bubble.querySelector('.bub').textContent};
      await sleep(700);
      const reply = [...document.querySelectorAll('#cc .im.assistant .txt')].pop().textContent.replace(/\\u00a0/g, ' ');
      const rows = [...document.querySelectorAll('.req-card .rc-pm-item')].map(r => ({
        size: (r.querySelector('.rc-pm-s') || {}).textContent || '', face: !!r.querySelector('.box-chip-face')}));
      return {during, reply, rows, pendingLeft: document.querySelectorAll('.im-pending').length,
              sent: window.__calls[window.__calls.length - 1], chipsLeft: chips.items('chat').length};
    })()""", timeout=60)
    assert first["during"]["pending"] == "Reading…", first
    assert "Face 32" in first["during"]["chip"] and first["during"]["words"].startswith("can you do"), first
    assert first["reply"] == REPLY_FACE and first["pendingLeft"] == 0, first
    assert first["rows"] == [{"size": "1 mm", "face": True}], first
    assert first["sent"]["faces"] == [FACE] and first["chipsLeft"] == 0, first
    # 2) a picture pasted, with words
    second = live.evaluate("""(async () => {
      const {A, sleep, type, paste, pic} = window.__t;
      paste([pic('crevice.png')]);
      await A.idle('chat');
      await type('in that crevice there I want a very refined mesh');
      await sleep(900);
      const bubble = [...document.querySelectorAll('#cc .im.user')].pop();
      return {img: !!bubble.querySelector('.att-img img'), sent: window.__calls[window.__calls.length - 1],
              reply: [...document.querySelectorAll('#cc .im.assistant .txt')].pop().textContent.replace(/\\u00a0/g, ' '),
              rows: document.querySelectorAll('.req-card .rc-pm-item').length};
    })()""", timeout=60)
    assert second["img"] and len(second["sent"]["attachments"]) == 1, second
    assert second["reply"] == REPLY_PIC and second["rows"] == 2, second
    _shot(live, "02_after_two_requirements.png")
    _shot(live, "03_narrow_panel.png", width=760, height=900)
    # 3) saying so starts the mesh, the same as the button
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
