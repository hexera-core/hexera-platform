# Responsibility: Verify the request card's "Start meshing" button: it shows while the approval question is pending
# (the brief's proceed_ask) on every route that asks it, it sends exactly what typing "yes" sends - the same
# request to the same route, the same "yes" in the conversation - and it is gone once the run starts or the
# question is no longer asked.
# Boundaries: the console's own modules in the page, through the real composer; only the chat route is stood in for.
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.ui

SESSION = "7b2e5c4d-1f0a-4c3b-9d8e-6a5f4e3d2c10"
ASK = "Shall I proceed with mesh generation? Reply yes to start it, or tell me what to change."
CFD = {"purpose": "internal flow", "engine": "snappy", "engine_label": "snappyHexMesh", "mesh_fidelity": "standard",
       "checks": [{"label": "Watertight", "status": "pass"}], "user_notes": [], "proceed_ask": ASK}
STARTED = {k: v for k, v in CFD.items() if k != "proceed_ask"}

# The chat route stood in for: the first message gets the plan and the question, a "yes" starts the run (`done`,
# with its job) and the card comes back without the question. Every request is kept exactly as sent.
STUB = """(async () => {
  const CARD = __CARD__, STARTED = __STARTED__;
  window.__sent = [];
  const real = window.fetch.bind(window);
  window.fetch = (u, o) => {
    if (String(u).includes('/api/v1/chat/message')) {
      window.__sent.push({url: String(u), method: o.method, body: o.body});
      const yes = JSON.parse(o.body).content === 'yes';
      const reply = yes ? {session_id: '__S__', reply: 'Starting the mesh.', brief: STARTED, __DONE__}
                        : {session_id: '__S__', reply: 'Here is the plan. __ASK__', brief: CARD};
      return Promise.resolve(new Response(JSON.stringify(reply), {status: 200, headers: {'Content-Type': 'application/json'}}));
    }
    return real(u, o);
  };
  const st = await import('/static/js/core/state.js');
  st.set.sessionId('__S__');
  const input = document.getElementById('chat-input');
  input.disabled = false; input.value = 'mesh the duct for internal flow';
  document.getElementById('send-btn').disabled = false;
  document.getElementById('send-btn').click();
})()"""


def _open(live, done: bool = False) -> None:
    live.evaluate(STUB.replace("__CARD__", json.dumps(CFD)).replace("__STARTED__", json.dumps(STARTED))
                  .replace("__DONE__", "done: true, job_id: 'job-start-1'" if done else "done: false")
                  .replace("__ASK__", ASK).replace("__S__", SESSION))
    live.wait_for("document.querySelector('.req-card .rc-go-btn')", timeout=30, what="the plan with its start button")


def _yes_bubbles(live) -> list:
    return live.evaluate("[...document.querySelectorAll('.im.user .bub')].map(b => b.textContent.trim())")


def test_the_button_shows_with_the_question_and_keeps_the_words_as_the_other_way(live):
    _open(live)
    got = live.evaluate("""(() => { const go = document.querySelector('.req-card .rc-go');
      return {buttons: go.querySelectorAll('button').length, label: go.querySelector('.rc-go-btn').textContent,
              alt: go.querySelector('.rc-go-alt').textContent,
              primary: getComputedStyle(go.querySelector('.rc-go-btn')).backgroundColor}; })()""")
    assert got == {"buttons": 1, "label": "Start meshing", "alt": "or tell me what to change",
                   "primary": "rgb(226, 224, 212)"}, got


def test_start_meshing_sends_exactly_what_typing_yes_sends(live, base_url):
    # the click
    _open(live)
    live.evaluate("document.querySelector('.req-card .rc-go-btn').click()")
    live.wait_for("window.__sent.length === 2", timeout=30, what="the yes to be sent")
    clicked = live.evaluate("window.__sent[1]")
    assert _yes_bubbles(live)[-1] == "yes"
    # the card comes back without the question: no button
    live.wait_for("!document.querySelector('.req-card .rc-go-btn')", timeout=30, what="the button to go")
    # the same card on a fresh page, the user typing yes instead
    live.open(f"{base_url}/ui", ready="document.getElementById('stage').children.length > 0")
    _open(live)
    live.evaluate("""(() => { const input = document.getElementById('chat-input');
      input.disabled = false; input.value = 'yes';
      document.getElementById('send-btn').disabled = false; document.getElementById('send-btn').click(); })()""")
    live.wait_for("window.__sent.length === 2", timeout=30, what="the typed yes to be sent")
    typed = live.evaluate("window.__sent[1]")
    assert _yes_bubbles(live)[-1] == "yes"
    assert clicked == typed, (clicked, typed)
    assert json.loads(clicked["body"]) == {"session_id": SESSION, "content": "yes"}
    assert clicked["method"] == "POST" and clicked["url"].endswith("/api/v1/chat/message")


def test_the_button_is_gone_once_the_run_starts(live):
    _open(live, done=True)
    live.evaluate("document.querySelector('.req-card .rc-go-btn').click()")
    live.wait_for("window.__sent.length === 2", timeout=30, what="the yes to be sent")
    live.wait_for("!document.querySelector('.rc-go-btn')", timeout=30, what="the button to go as the run starts")


def test_every_route_that_asks_gets_the_button_and_none_without_the_question(live):
    out = live.evaluate("""(async () => {
      const { Stage } = await import('/static/js/render/stage.js');
      const seen = {};
      const start = async () => {};
      for (const [k, b] of Object.entries({
          cfd: {purpose: 'internal flow', engine: 'snappy', proceed_ask: 'Shall I proceed?'},
          fea: {purpose: 'structural', engine: 'gmsh', mesh_as: 'shell', proceed_ask: 'Shall I proceed?'},
          thermal: {check_mode: 'thermal', job_sheet: 'A set-top box.', engine: 'snapgrid', proceed_ask: 'Shall I proceed?'},
          asked_nothing: {purpose: 'internal flow', engine: 'snappy'}})) {
        Stage.mount();
        Stage.brief(b, async () => {}, null, start);
        seen[k] = document.querySelectorAll('.rc-go-btn').length;
      }
      return seen; })()""")
    assert out == {"cfd": 1, "fea": 1, "thermal": 1, "asked_nothing": 0}, out
