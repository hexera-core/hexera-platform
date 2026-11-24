# Responsibility: Verify a user can attach pictures and files to the chat box - the paper-clip,
#   Ctrl+V of a screenshot, a drop on the box - see each as a chip, take one off, hear the limits in one plain line,
#   send a picture alone, and see what they sent in the conversation.
# Boundaries: the console's own modules in the page; the server's routes are stubbed in-page. It reads the DOM.
from __future__ import annotations

import base64
import json
import os

import pytest
from conftest import assert_clean

pytestmark = pytest.mark.ui

# THE PAGE'S SERVER, STUBBED: every attachment upload, message and stored file is answered here and recorded.
SETUP = """
  const S = await import('/static/js/core/state.js');
  const C = await import('/static/js/shell/composer.js');
  const A = await import('/static/js/shell/attach.js');
  const chips = await import('/static/js/shell/chips.js');
  const { Stage } = await import('/static/js/render/stage.js');
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const png = await new Promise(r => { const c = document.createElement('canvas'); c.width = 8; c.height = 6;
    c.getContext('2d').fillRect(0, 0, 4, 3); c.toBlob(r, 'image/png'); });
  const pic = (name) => new File([png], name || 'image.png', {type: 'image/png'});
  window.__calls = [];
  let n = 0;
  const real = window.fetch;
  window.fetch = async (url, opts = {}) => {
    const u = String(url);
    if (u.startsWith('/api/v1/chat/attachments/')) { window.__calls.push(['get', u]);
      return new Response(png, {status: 200, headers: {'Content-Type': 'image/png'}}); }
    if (u === '/api/v1/chat/attachments') {
      const f = opts.body.get('file'); n += 1; window.__calls.push(['upload', f.name]);
      await sleep(80);
      const kind = f.type.startsWith('image/') ? 'image' : 'text';
      return new Response(JSON.stringify({id: ('a' + n).padEnd(16, '0'), kind, name: f.name, mime: f.type,
        bytes: f.size, pages: kind === 'text' ? 3 : null}), {status: 200, headers: {'Content-Type': 'application/json'}});
    }
    if (u === '/api/v1/chat/message') { window.__calls.push(['message', JSON.parse(opts.body)]);
      return new Response(JSON.stringify({reply: 'Noted.', choices: []}),
        {status: 200, headers: {'Content-Type': 'application/json'}}); }
    if (u.startsWith('/api/')) { window.__calls.push(['other', u]);
      return new Response('{}', {status: 200, headers: {'Content-Type': 'application/json'}}); }
    return real(url, opts);
  };
  Stage.mount();
  S.set.sessionId('s-attach');
  C.enableInput();
  const paste = (el, files, text) => { const dt = new DataTransfer(); files.forEach(f => dt.items.add(f));
    if (text) dt.setData('text/plain', text);
    const e = new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true});
    el.dispatchEvent(e); return e.defaultPrevented; };
  const drop = (el, files) => { const dt = new DataTransfer(); files.forEach(f => dt.items.add(f));
    el.dispatchEvent(new DragEvent('dragover', {dataTransfer: dt, bubbles: true, cancelable: true}));
    el.dispatchEvent(new DragEvent('drop', {dataTransfer: dt, bubbles: true, cancelable: true})); };
  const row = (box) => [...document.querySelectorAll(`.box-chips[data-box="${box}"] .box-chip`)].map(c => ({
    cls: c.className, label: (c.querySelector('.box-chip-label') || {}).textContent || '',
    thumb: !!c.querySelector('img.box-chip-thumb')}));
  const line = () => document.querySelector('#input-bar > .box-msg').textContent;
"""


def test_the_chat_box_takes_a_pasted_picture_a_dropped_file_and_says_its_limits(live):
    out = live.evaluate("(async () => {" + SETUP + """
      const inp = document.getElementById('chat-input');
      const clip = document.querySelector('#input-inner > .attach-clip');
      const before = {clip: !!clip, tip: clip && clip.title, chips: row('chat'),
                      first: clip.nextElementSibling.id};
      // words on the clipboard are pasted as words
      const textPaste = paste(inp, [], 'mesh it at 2 mm');
      // a screenshot pasted: a chip with its thumbnail, uploading, then held
      const took = paste(inp, [pic('image.png')]);
      const going = row('chat'); const busy = chips.busy('chat');
      await A.idle('chat');
      const held = row('chat');
      // a file dropped on the box is attached, never taken as the CAD upload
      drop(document.getElementById('input-bar'), [new File(['bore 20 mm'], 'spec.txt', {type: 'text/plain'})]);
      await A.idle('chat');
      const withFile = row('chat');
      // remove the picture
      document.querySelector('.box-chips[data-box="chat"] .box-chip-image .box-chip-x').click();
      const afterRemove = row('chat');
      // a kind that cannot be attached; a picture too large; then more than five
      drop(document.getElementById('input-bar'), [new File(['MZ'], 'tool.exe', {type: 'application/x-msdownload'})]);
      const kindLine = line();
      inp.dispatchEvent(new Event('input'));
      const cleared = line();
      paste(inp, [new File([new Uint8Array(11 * 1024 * 1024)], 'huge.png', {type: 'image/png'})]);
      const bigLine = line();
      paste(inp, [1, 2, 3, 4, 5].map(i => pic('p' + i + '.png')));
      await A.idle('chat');
      return {before, textPaste, took, going, busy, held, withFile, afterRemove, kindLine, cleared, bigLine,
              limitLine: line(), count: row('chat').length, calls: window.__calls};
    })()""")
    assert out["before"]["clip"] and out["before"]["tip"] == "Attach a picture or a file. It is sent to the AI with your words.", out
    assert out["before"]["first"] == "chat-input" and out["before"]["chips"] == [], out
    assert out["textPaste"] is False, "words on the clipboard were taken as a file"
    assert out["took"] is True and out["busy"] is True, out
    assert len(out["going"]) == 1 and "is-pending" in out["going"][0]["cls"] and out["going"][0]["thumb"], out
    assert "is-pending" not in out["held"][0]["cls"] and out["held"][0]["thumb"], out
    assert [c["label"] for c in out["withFile"]] == ["", "spec.txt · 3 pages"], out
    assert [c["label"] for c in out["afterRemove"]] == ["spec.txt · 3 pages"], out
    assert out["kindLine"] == "That kind of file cannot be attached. Pictures, PDF and text files can.", out
    assert out["cleared"] == "", "the line stays after the user typed"
    assert out["bigLine"] == "That picture is too large: 10 MB is the most.", out
    assert out["limitLine"] == "Only 5 files per message." and out["count"] == 5, out
    uploads = [c[1] for c in out["calls"] if c[0] == "upload"]
    assert uploads == ["image.png", "spec.txt", "p1.png", "p2.png", "p3.png", "p4.png"], uploads
    assert not [c for c in out["calls"] if c[0] == "other"], "a drop on the box reached the CAD upload"
    assert_clean(live, "attaching in the chat box")


def test_a_picture_alone_is_sent_and_shows_in_the_users_bubble(live):
    out = live.evaluate("(async () => {" + SETUP + """
      const inp = document.getElementById('chat-input');
      paste(inp, [pic('bore.png')]);
      // Send pressed while the picture is still uploading: the send waits for it
      document.getElementById('send-btn').click();
      for (let i = 0; i < 50 && !window.__calls.some(c => c[0] === 'message'); i++) await sleep(40);
      await sleep(120);
      const bubble = document.querySelector('#cc .im.user');
      return {sent: window.__calls.filter(c => c[0] === 'message').map(c => c[1]), chips: row('chat'),
              thumb: bubble && bubble.querySelector('.att-img img') && bubble.querySelector('.att-img img').src,
              words: bubble && !!bubble.querySelector('.bub')};
    })()""")
    assert len(out["sent"]) == 1, out
    msg = out["sent"][0]
    assert msg["content"] == "" and msg["attachments"] == ["a100000000000000"] and msg["session_id"] == "s-attach", msg
    assert out["chips"] == [], "the chip stayed after the send"
    assert out["thumb"] and out["thumb"].startswith("blob:"), out
    assert out["words"] is False, "an empty bubble was drawn under the picture"
    assert_clean(live, "sending a picture alone")


# A SENT PICTURE IS SEEN, NOT GUESSED AT: at most 240 px wide and 180 px high by its own shape, never stretched
# past its own size, the same in the bubble and in the notes, and a click opens it whole.
SIZES = {"a1": (1600, 1200, 240, 180), "a2": (2000, 400, 240, 48), "a3": (300, 900, 60, 180), "a4": (100, 60, 100, 60)}


def test_a_sent_picture_is_shown_large_enough_to_read_and_opens_on_a_click(live):
    out = live.evaluate("(async () => {" + SETUP + """
      const SIZES = __SIZES__, blobs = {};
      for (const [k, [w, h]] of Object.entries(SIZES)) blobs[k.padEnd(16, '0')] = await new Promise(r => {
        const c = document.createElement('canvas'); c.width = w; c.height = h; const g = c.getContext('2d');
        g.fillStyle = '#4a6'; g.fillRect(0, 0, w, h); c.toBlob(r, 'image/png'); });
      const stubbed = window.fetch;
      window.fetch = async (url, opts) => { const id = String(url).split('/').pop();
        return blobs[id] ? new Response(blobs[id], {status: 200, headers: {'Content-Type': 'image/png'}}) : stubbed(url, opts); };
      const rows = Object.keys(SIZES).map(k => ({id: k.padEnd(16, '0'), kind: 'image', name: k + '.png'}));
      Stage.brief({purpose: 'structural', engine: 'gmsh', user_notes: [{text: 'see the pictures', attachments: rows}]}, () => {});
      Stage.chat('user', 'see the pictures', undefined, {attachments: rows, sid: 's-attach'});
      const imgs = () => [...document.querySelectorAll('.att-img img')];
      for (let i = 0; i < 100 && !(imgs().length === 8 && imgs().every(m => m.complete && m.naturalWidth)); i++) await sleep(30);
      await sleep(50);
      const look = (root) => [...root.querySelectorAll('.att-img')].map(a => { const m = a.querySelector('img'),
        r = m.getBoundingClientRect(), box = root.getBoundingClientRect(), f = a.getBoundingClientRect();
        return {w: Math.round(r.width), h: Math.round(r.height), nat: m.naturalWidth,
                hugs: f.width - r.width <= 2.5 && f.height - r.height <= 2.5,
                round: parseFloat(getComputedStyle(a).borderTopLeftRadius) > 0, inside: r.right <= box.right + 0.5}; });
      const note = document.querySelector('.rc-notes-list li.rc-note'), bubble = document.querySelector('#cc .im.user');
      const wide = {note: look(note), bubble: look(bubble)};
      const nr = note.getBoundingClientRect();
      window.__noteRect = {x: nr.left, y: nr.top, width: nr.width, height: nr.height, scale: 1};
      // A NARROW PANEL: the pictures stay inside the note
      const host = document.getElementById('stage'), was = host.style.width;
      host.style.width = '260px'; await sleep(50);
      const narrow = look(note);
      host.style.width = was;
      // A CLICK opens the stored picture whole, in a new tab
      const opened = []; const realOpen = window.open; window.open = (u, t) => { opened.push([u, t]); return null; };
      note.querySelector('.att-img').click(); await sleep(50);
      window.open = realOpen;
      return {wide, narrow, opened, src: note.querySelector('.att-img img').src};
    })()""".replace("__SIZES__", json.dumps(SIZES)))
    if os.environ.get("THUMB_SHOTS"):              # the note as drawn, kept for a look by eye
        shot = live._send("Page.captureScreenshot", {"format": "png", "clip": live.evaluate("window.__noteRect"),
                                                     "captureBeyondViewport": True})
        os.makedirs(os.environ["THUMB_SHOTS"], exist_ok=True)
        with open(os.path.join(os.environ["THUMB_SHOTS"], "note_pictures.png"), "wb") as fh:
            fh.write(base64.b64decode(shot["data"]))
    want = [{"w": w, "h": h} for _, _, w, h in SIZES.values()]
    for where in ("note", "bubble"):
        got = out["wide"][where]
        assert [{"w": g["w"], "h": g["h"]} for g in got] == want, (where, got)
        # the rounded frame hugs the picture: no empty band beside or under it
        assert all(g["round"] and g["inside"] and g["hugs"] for g in got), (where, got)
        assert [g["nat"] for g in got] == [w for w, _, _, _ in SIZES.values()], "the stored picture was not the one drawn"
    assert all(g["inside"] and g["hugs"] and g["w"] <= 240 for g in out["narrow"]), out["narrow"]
    assert out["opened"] == [[out["src"], "_blank"]], out
    assert_clean(live, "a sent picture's size")


def test_a_refused_send_keeps_the_chips_and_the_words_and_says_why_under_the_box(live):
    out = live.evaluate("(async () => {" + SETUP + """
      const stubbed = window.fetch;
      window.fetch = async (url, opts = {}) => String(url) === '/api/v1/chat/message'
        ? new Response(JSON.stringify({detail: 'That picture is no longer here. Attach it again, then send.'}),
                       {status: 422, headers: {'Content-Type': 'application/json'}})
        : stubbed(url, opts);
      const inp = document.getElementById('chat-input');
      paste(inp, [pic('bore.png')]);
      await A.idle('chat');
      inp.value = '2 mm on this bore';
      document.getElementById('send-btn').click();
      await sleep(200);
      return {chips: row('chat').length, words: inp.value, line: line(), open: !inp.disabled,
              bubbles: document.querySelectorAll('#cc .im.user').length};
    })()""")
    assert out["chips"] == 1 and out["words"] == "2 mm on this bore", out
    assert out["line"] == "That picture is no longer here. Attach it again, then send.", out
    assert out["open"] and out["bubbles"] == 0, out


def test_chips_tell_a_listener_of_every_change_a_silent_clear_too(live):
    out = live.evaluate("""(async () => {
      const chips = await import('/static/js/shell/chips.js');
      const heard = [];
      const off = chips.listen((box, items) => heard.push([box, items.length]));
      const k = chips.add('lx', {kind: 'face', label: 'Face 12', value: {id: 'Face_12', label: 'Face 12'}});
      chips.add('lx', {kind: 'face', label: 'Face 3', value: {id: 'Face_3', label: 'Face 3'}});
      chips.remove('lx', k);
      chips.clear('lx');
      off();
      chips.add('lx', {kind: 'face', label: 'Face 9'});
      return heard;
    })()""")
    assert out == [["lx", 1], ["lx", 2], ["lx", 1], ["lx", 0]], out


def test_a_reloaded_conversation_shows_a_picture_sent_alone(live):
    out = live.evaluate("(async () => {" + SETUP + """
      const { intakeOrder } = await import('/static/js/core/conversation.js');
      const items = intakeOrder([{role: 'assistant', content: 'What do you want?'},
        {role: 'user', content: '', attachments: [{id: 'a100000000000000', kind: 'image', name: 'bore.png'}]},
        {role: 'user', content: ''}]);
      for (const it of items) Stage.chat(it.role, it.text, undefined, it.extra ? {...it.extra, sid: 's-old'} : null);
      await sleep(150);
      const u = document.querySelector('#cc .im.user .att-img');
      return {n: items.length, sid: u && u.dataset.sid, src: u && u.querySelector('img').src,
              got: window.__calls.filter(c => c[0] === 'get').map(c => c[1])};
    })()""")
    assert out["n"] == 2, "a picture-only turn was dropped, or an empty one drawn"
    assert out["sid"] == "s-old" and out["src"].startswith("blob:"), out
    assert out["got"] == ["/api/v1/chat/attachments/s-old/a100000000000000"], out
    assert_clean(live, "a reloaded picture")
