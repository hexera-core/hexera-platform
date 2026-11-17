# Responsibility: Verify the run box is an item of the conversation (Areen, 2026-11-16): a run starts below the
# message that started it with every earlier message still there, a reload (or the run re-opened) draws the same
# order, a re-run's boxes each sit where their run began, the composer says a run is going while it is, and the
# conversation stays where the user scrolled while the run updates.
# Boundaries: the page's own wiring (ui/js/main.js, a real reload through ?job=); only the network is stood in for.
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

pytestmark = pytest.mark.ui

RUNNING = "Generating the mesh…"

# The network a run page talks to, stood in for: the job reads, the conversation's history, the chat's replies
# (in order) and no live stream (its ticket is refused, as on a page whose socket cannot open). Installed with
# `Page.addScriptToEvaluateOnNewDocument` it also holds across a real reload.
STUB = r"""(() => {
  window.__RB = __CFG__;
  const json = (x, s) => Promise.resolve(new Response(JSON.stringify(x),
    {status: s || 200, headers: {'Content-Type': 'application/json'}}));
  const real = window.fetch.bind(window);
  window.fetch = (u, o) => {
    const s = String(u), c = window.__RB;
    if (s.includes('/api/v1/ws/ticket')) return json({detail: 'no stream here'}, 404);
    if (s.includes('/api/v1/chat/message')) return json(c.replies.shift());
    let m = s.match(/\/api\/v1\/chat\/history\/([^/?]+)/);
    if (m) return json(c.history[m[1]] || {messages: []});
    m = s.match(/\/api\/v1\/simulation\/([^/?]+)(\/[^?]*)?/);
    if (m && !m[2] && c.jobs[m[1]]) return json(c.jobs[m[1]]);
    if (m) return json({detail: 'not here'}, 404);
    return real(u, o);
  };
})();"""

# What the conversation column shows, top to bottom: each message as "U:"/"A:" and its words, each run box as
# "BOX:" and its title; the composer's placeholder; how many boxes carry the live ids.
READ = r"""(() => {
  const words = (el) => (el.querySelector('.bub, .txt') || el).innerText.replace(/\s+/g, ' ').trim();
  const order = [...document.getElementById('cc').children].map((el) =>
    el.classList.contains('proc') ? 'BOX:' + el.querySelector('.proc-title').textContent
    : el.classList.contains('im') && el.classList.contains('user') ? 'U:' + words(el)
    : el.classList.contains('im') ? 'A:' + words(el) : '').filter(Boolean);
  const inp = document.getElementById('chat-input');
  return {order, placeholder: inp.placeholder, disabled: inp.disabled,
          liveIds: document.querySelectorAll('#tl').length,
          liveInLast: !!document.querySelector('#cc .proc:last-of-type #tl'),
          text: document.getElementById('cc').innerText};
})()"""

INTAKE = ["Geometry received. What are you meshing this for?", "static stress, about 800k elements",
          "Here is the plan. Shall I proceed?"]


def _ago(seconds: int) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _job(job: str, session: str, status: str = "running") -> dict:
    out = {"id": job, "status": status, "current_attempt": 1, "created_at": _ago(95), "started_at": _ago(90),
           "session_id": session, "geometry_filename": "bracket.step", "artifacts": []}
    if status != "running":
        out["ended_at"] = _ago(20)
    return out


def _history(session: str, messages: list[dict]) -> dict:
    return {"session_id": session, "messages": messages}


def _stub(cfg: dict) -> str:
    return STUB.replace("__CFG__", json.dumps(cfg))


def _talk(live, session: str, lines: list[str]) -> None:
    """The intake as it was drawn live: Hexera and the user in turn, the page holding the session."""
    live.evaluate("""(async () => {
      const st = await import('/static/js/core/state.js');
      const { Stage } = await import('/static/js/render/stage.js');
      st.set.sessionId('__S__');
      __LINES__.forEach((t, i) => Stage.chat(i % 2 ? 'user' : 'assistant', t));
    })()""".replace("__S__", session).replace("__LINES__", json.dumps(lines)))


def _say(live, text: str) -> None:
    live.evaluate("""(async () => {
      const c = await import('/static/js/shell/composer.js');
      await c.say(__T__);
    })()""".replace("__T__", json.dumps(text)), timeout=30)


def _reload(live, base_url: str, cfg: dict, job: str, ready: str) -> None:
    live._send("Page.addScriptToEvaluateOnNewDocument", {"source": _stub(cfg)})
    live.open(f"{base_url}/ui?job={job}", ready=ready)


def _kinds(order: list[str]) -> list[str]:
    """The user's messages and the run boxes, in order: what decides where each run sits."""
    return [o if o.startswith("U:") else "BOX" for o in order if o.startswith(("U:", "BOX:"))]


def test_a_run_starts_below_the_message_that_started_it_and_a_reload_draws_the_same(live, base_url):
    job, session = "rb-one", "rb-sess-one"
    said = [{"role": "assistant", "content": INTAKE[0]}, {"role": "user", "content": INTAKE[1]},
            # never shown, live or reloaded: the application's own nudge and a turn with no words
            {"role": "user", "content": "Close the conversation now.", "_synthetic": True},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
            {"role": "assistant", "content": INTAKE[2]},
            {"role": "user", "content": "yes, proceed", "job_id": job}]
    cfg = {"jobs": {job: _job(job, session)}, "history": {session: _history(session, said)},
           "replies": [{"session_id": session, "done": True, "job_id": job,
                        "reply": f"Mesh generation accepted and queued for bracket.step. Job ID: {job}"}]}
    live.evaluate(_stub(cfg))
    _talk(live, session, INTAKE)
    _say(live, "yes, proceed")
    live.wait_for("document.querySelector('#cc .proc')", what="the run box")
    seen = live.evaluate(READ)
    # THE CONVERSATION STAYS: every earlier message, then the user's yes, then the box
    assert seen["order"] == ["A:" + INTAKE[0], "U:" + INTAKE[1], "A:" + INTAKE[2], "U:yes, proceed",
                             "BOX:Generating mesh"], seen["order"]
    # the box is the answer: no "Job ID" line the reload could never draw again
    assert "Job ID" not in seen["text"], seen["text"]
    assert seen["placeholder"] == RUNNING and seen["disabled"], seen

    _reload(live, base_url, cfg, job,
            ready="document.querySelectorAll('#cc .im').length >= 4 && document.querySelector('#cc .proc')")
    again = live.evaluate(READ)
    assert again["order"] == seen["order"], again["order"]
    assert again["placeholder"] == RUNNING and again["disabled"], again


def test_a_run_reopened_without_its_mark_still_comes_after_the_conversation(live, base_url):
    # a conversation stored before runs were marked in it: the box goes after everything said
    job, session = "rb-old", "rb-sess-old"
    said = [{"role": "assistant", "content": INTAKE[0]}, {"role": "user", "content": "yes, proceed"}]
    cfg = {"jobs": {job: _job(job, session, "failed")}, "history": {session: _history(session, said)}, "replies": []}
    _reload(live, base_url, cfg, job, ready="document.querySelector('#cc .proc.done')")
    order = live.evaluate(READ)["order"]
    assert order[:3] == ["A:" + INTAKE[0], "U:yes, proceed", "BOX:Run failed"], order


def test_each_run_of_a_conversation_sits_where_it_started_live_and_reloaded(live, base_url):
    first, second, session = "rb-first", "rb-second", "rb-sess-two"
    said = [{"role": "assistant", "content": INTAKE[0]}, {"role": "user", "content": INTAKE[1]},
            {"role": "assistant", "content": INTAKE[2]},
            {"role": "user", "content": "yes, proceed", "job_id": first},
            {"role": "user", "content": "Run it again with the same requirements.", "job_id": second}]
    cfg = {"jobs": {first: _job(first, session), second: _job(second, session)},
           "history": {session: _history(session, said)},
           "replies": [{"session_id": session, "done": True, "job_id": first, "reply": "queued"},
                       {"session_id": session, "done": True, "job_id": second, "reply": "queued"}]}
    live.evaluate(_stub(cfg))
    _talk(live, session, INTAKE)
    _say(live, "yes, proceed")
    live.wait_for("document.querySelector('#cc .proc')", what="the first run's box")
    # the first run ends (the poller reads it), and the conversation goes on under it
    live.evaluate("""(() => { Object.assign(window.__RB.jobs['__J__'],
      {status: 'failed', ended_at: new Date().toISOString()}); })()""".replace("__J__", first))
    live.wait_for("document.querySelector('#cc .proc.done') && !document.getElementById('chat-input').disabled",
                  timeout=30, what="the first run to end")
    _say(live, "Run it again with the same requirements.")
    live.wait_for("document.querySelectorAll('#cc .proc').length === 2", what="the second run's box")
    seen = live.evaluate(READ)
    assert _kinds(seen["order"]) == ["U:" + INTAKE[1], "U:yes, proceed", "BOX",
                                     "U:Run it again with the same requirements.", "BOX"], seen["order"]
    assert seen["order"][seen["order"].index("U:yes, proceed") + 1] == "BOX:Run failed", seen["order"]
    # one box is live: the newest; the first keeps what it said
    assert seen["liveIds"] == 1 and seen["liveInLast"], seen

    cfg["jobs"][first] = _job(first, session, "failed")
    _reload(live, base_url, cfg, second, ready="document.querySelectorAll('#cc .proc').length === 2")
    again = live.evaluate(READ)
    assert _kinds(again["order"]) == _kinds(seen["order"]), again["order"]
    assert again["order"][again["order"].index("U:yes, proceed") + 1] == "BOX:Run failed", again["order"]
    assert again["liveIds"] == 1 and again["liveInLast"], again


def test_the_first_run_reopened_shows_its_conversation_and_not_the_next_runs(live, base_url):
    first, second, session = "rb-a", "rb-b", "rb-sess-three"
    said = [{"role": "assistant", "content": INTAKE[0]}, {"role": "user", "content": "yes, proceed", "job_id": first},
            {"role": "assistant", "content": "It took 2 minutes."},
            {"role": "user", "content": "Run it again with the same requirements.", "job_id": second}]
    cfg = {"jobs": {first: _job(first, session, "failed"), second: _job(second, session)},
           "history": {session: _history(session, said)}, "replies": []}
    _reload(live, base_url, cfg, first, ready="document.querySelector('#cc .proc.done') && "
            "document.getElementById('cc').innerText.includes('It took 2 minutes.')")
    order = live.evaluate(READ)["order"]
    # what was said after the run comes after its result; the next run (and its "run again") is its own page
    assert _kinds(order) == ["U:yes, proceed", "BOX"], order
    assert order.index("A:It took 2 minutes.") > order.index("BOX:Run failed"), order


def test_the_conversation_stays_where_the_user_scrolled_while_the_run_updates(live):
    out = live.evaluate("""(async () => {
      const { Stage } = await import('/static/js/render/stage.js');
      const { dispatch } = await import('/static/js/core/events.js');
      const frame = () => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
      const s = () => document.querySelector('#stage .scroll');
      const atEnd = () => s().scrollHeight - s().scrollTop - s().clientHeight < 4;
      Stage.mount();
      for (let i = 0; i < 30; i++) Stage.chat(i % 2 ? 'user' : 'assistant', 'Message ' + i + ' of the intake.');
      Stage.newRun(); Stage.ensureProc();
      await frame();
      const shown = atEnd();                             // the box appeared: it is in view
      s().scrollTop = 0; await frame();                  // the user scrolls up to read
      let c = null;
      for (let i = 0; i < 12; i++) c = dispatch({type: 'narrate', stage: 'builder', kind: 'say', text: 'Decision ' + i}, Stage, c);
      await frame();
      const kept = s().scrollTop;
      s().scrollTop = s().scrollHeight; await frame();   // back at the bottom: it follows again
      for (let i = 0; i < 12; i++) c = dispatch({type: 'narrate', stage: 'builder', kind: 'step', text: 'Step ' + i}, Stage, c);
      Stage.chat('assistant', 'One more line.');
      await frame();
      return {shown, kept, follows: atEnd()};
    })()""")
    assert out == {"shown": True, "kept": 0, "follows": True}, out
