# Responsibility: Verify "Send change" on a delivered mesh in a real browser: mark a spot, type the change, send; Hexera's reply is in the chat; while the new run works the old mesh stays on screen, usable, with one progress line; when the run delivers, its mesh takes the old one's place on the same camera with the marks gone; a reply that starts no run leaves the marks and the words; a failed run leaves the old mesh.
# Boundaries: the page's own wiring (ui/js/main.js and the shipped viewer); only the network is stood in for, per the console contract (POST /simulation/{id}/dispute, mode rebuild, answers {kind, reply, job_id, session_id}).
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import assert_clean
from test_heatmap import _payload

pytestmark = pytest.mark.ui

OLD, NEW = "rc-old", "rc-new"
QUESTION = "Which side of the outlet do you mean?"
STARTED = "I will make the cells finer at your mark. A new run has started."

STUB = r"""(() => {
  window.__RC = __CFG__;
  window.__asks = [];
  const json = (x, s) => Promise.resolve(new Response(JSON.stringify(x),
    {status: s || 200, headers: {'Content-Type': 'application/json'}}));
  const real = window.fetch.bind(window);
  window.fetch = (u, o) => {
    const s = String(u), c = window.__RC;
    if (s.includes('/api/v1/ws/ticket')) return json({detail: 'no stream here'}, 404);
    if (s.includes('/client-config')) return json({});
    if (s.includes('/simulation/__OLD__/surface')) return json(c.old);
    if (s.includes('/simulation/__NEW__/surface')) return json(c.neu);
    if (s.includes('/simulation/__OLD__/dispute')) { window.__asks.push(JSON.parse(o.body)); return json(c.answers.shift()); }
    const m = s.match(/\/api\/v1\/simulation\/([^/?]+)(\/[^?]*)?/);
    if (m && !m[2] && c.jobs[m[1]]) return json(c.jobs[m[1]]);
    if (m) return json({detail: 'not here'}, 404);
    return real(u, o);
  };
})();""".replace("__OLD__", OLD).replace("__NEW__", NEW)

PANEL = r"""((j) => {
  const v = document.getElementById('viewer-' + j);
  if (!v) return null;
  const prog = document.getElementById('v-prog-' + j);
  return {shown: !v.hidden && v.getBoundingClientRect().width > 0,
          send: document.getElementById('v-submit-' + j).textContent,
          sendOff: document.getElementById('v-submit-' + j).disabled,
          ask: document.getElementById('v-ask-' + j).value,
          placeholder: document.getElementById('v-ask-' + j).placeholder,
          progress: prog.hidden ? '' : prog.textContent,
          keys: document.getElementById('v-keys-' + j).innerText,
          markers: window._vdbg[j] ? window._vdbg[j].markers() : null,
          text: v.querySelector('.v-review').innerText,
          modal: !!document.querySelector('.v-modal'),
          overlay: !!document.querySelector('#workbench .v-loading')};
})"""


def _ago(seconds: int) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _setup(live, tmp_path, answers: list[dict]) -> None:
    old = _payload(tmp_path / "old")
    # the revised mesh: the same part, its numbers changed (same box, so the camera carries over whole)
    neu = json.loads(json.dumps(old))
    neu["quality"].update(cells=24, max_non_ortho=12.0)
    neu["cell_count"] = 24
    job = {"id": NEW, "status": "running", "current_attempt": 1, "created_at": _ago(30),
           "started_at": _ago(25), "artifacts": []}
    cfg = {"old": old, "neu": neu, "answers": answers, "jobs": {NEW: job}}
    live.evaluate(STUB.replace("__CFG__", json.dumps(cfg)))
    live.evaluate("""(async () => {
      const { openViewer } = await import('/static/js/viewer/viewer.js');
      await openViewer('__J__', document.getElementById('stage'), {metrics: {pass: true}, files: []});
    })()""".replace("__J__", OLD), timeout=60)
    live.wait_for(f"window._vdbg && window._vdbg['{OLD}:heat'] && document.querySelector('#v-keys-{OLD} dl')",
                  timeout=90, what="the delivered mesh")


def _panel(live, job: str):
    return live.evaluate(f"{PANEL}({json.dumps(job)})")


def _mark_and_type(live, words: str) -> None:
    live.evaluate(f"""(() => {{
      window._vdbg['{OLD}:heat'].probe('outlet', 0);
      document.querySelector('#viewer-{OLD} .v-probe [data-a=mark]').click();
      const ta = document.getElementById('v-ask-{OLD}');
      ta.value = {json.dumps(words)}; ta.dispatchEvent(new Event('input'));
    }})()""")


def _send(live) -> None:
    live.evaluate(f"document.getElementById('v-submit-{OLD}').click()")


def _chat(live) -> str:
    return live.evaluate("document.getElementById('cc').innerText")


def _finish(live, status: str, closing: str) -> None:
    # the run's closing words, as its stream's closing event leaves them, then the job's end
    live.evaluate(f"""(async () => {{
      const st = await import('/static/js/core/state.js');
      st.set.outcomeMessage({json.dumps(closing)});
      Object.assign(window.__RC.jobs['{NEW}'], {{status: {json.dumps(status)}, ended_at: new Date().toISOString()}});
    }})()""")


def test_send_change_keeps_the_old_mesh_while_the_run_works_and_shows_the_new_one_when_done(live, tmp_path):
    _setup(live, tmp_path, [{"kind": "ask", "reply": QUESTION, "job_id": None},
                            {"kind": "change", "reply": STARTED, "job_id": NEW, "session_id": None}])
    p = _panel(live, OLD)
    # ONE BOX AND ONE BUTTON in the panel: nothing marked and nothing typed keeps it off
    assert p["send"] == "Send change" and p["sendOff"] and p["placeholder"] == "What should change here?", p
    assert "Mark region" in p["text"] and "Clear marks" in p["text"], p
    for gone in ("reviewer", "pipeline", "compute", "Request a change", "few minutes"):
        assert gone not in p["text"], (gone, p["text"])

    # A QUESTION BACK starts nothing: its words are in the chat, the marks and the words stay
    _mark_and_type(live, "finer cells here")
    assert not _panel(live, OLD)["sendOff"]
    _send(live)
    live.wait_for(f"document.getElementById('cc').innerText.includes({json.dumps(QUESTION)})", what="the question back")
    p = _panel(live, OLD)
    assert p["markers"] == 1 and p["ask"] == "finer cells here" and not p["sendOff"] and not p["progress"], p
    assert not p["modal"], p
    ask = live.evaluate("window.__asks[0]")
    assert ask["comment"] == "finer cells here" and ask["mode"] == "rebuild" and len(ask["flags"]) == 1, ask
    assert {"x", "y", "z", "span", "patch"} <= set(ask["flags"][0]), ask

    # SEND AGAIN: a run starts. Its reply is Hexera's in the chat; the old mesh stays, marks and all,
    # with one progress line, and the tools still work on it
    _send(live)
    live.wait_for(f"document.getElementById('cc').innerText.includes({json.dumps(STARTED)})", what="the run's reply")
    live.wait_for("document.querySelector('#cc .proc')", what="the new run's box")
    p = _panel(live, OLD)
    assert p["shown"] and p["markers"] == 1 and p["sendOff"], p
    assert p["progress"].startswith("Rebuilding with your change..."), p
    assert not p["overlay"] and not p["modal"] and _panel(live, NEW) is None, p
    tools = live.evaluate(f"""(() => {{ const t = window._vdbg['{OLD}:tools'];
      t.open('measure'); const m = t.current(); t.close(); t.open('section'); const s = t.current(); t.close();
      return [m, s]; }})()""")
    assert tools == ["measure", "section"], tools
    cam = live.evaluate(f"window._vdbg['{OLD}:tools'].camera()")
    oldKeys = p["keys"]

    # THE RUN DELIVERS: its mesh takes the old one's place, on the same camera, with no marks
    _finish(live, "succeeded", "Mesh ready. The cells at your mark are finer now.")
    live.wait_for(f"window._vdbg['{NEW}:tools'] && document.querySelector('#v-keys-{NEW} dl') "
                  f"&& !document.getElementById('viewer-{OLD}')", timeout=60, what="the revised mesh")
    n = _panel(live, NEW)
    assert n["shown"] and n["markers"] == 0 and not n["progress"] and n["ask"] == "", n
    assert n["keys"] != oldKeys, (oldKeys, n["keys"])
    now = live.evaluate(f"window._vdbg['{NEW}:tools'].camera()")
    # the same camera; the viewer's own camera envelope (the orbit target kept in the part's box) may
    # nudge it by a hair, as it would the old view on its next move
    for k in ("position", "focal", "up"):
        assert now[k] == pytest.approx(cam[k], abs=0.05), (k, cam, now)
    assert now["angle"] == cam["angle"] and now["parallel"] == cam["parallel"], (cam, now)
    assert "The cells at your mark are finer now." in _chat(live)
    assert_clean(live, "the change request")


def test_a_failed_run_leaves_the_old_mesh_and_says_so(live, tmp_path):
    _setup(live, tmp_path, [{"kind": "change", "reply": STARTED, "job_id": NEW}])
    _mark_and_type(live, "")
    _send(live)
    live.wait_for(f"document.querySelector('#cc .proc') && !document.getElementById('v-prog-{OLD}').hidden",
                  what="the run under way")
    _finish(live, "failed", "The new run could not build the mesh: the finer cells did not fit.")
    live.wait_for(f"document.getElementById('cc').innerText.includes('the finer cells did not fit')",
                  timeout=30, what="the failure in the chat")
    p = _panel(live, OLD)
    assert p["shown"] and not p["progress"] and not p["sendOff"] and p["markers"] == 1, p
    assert _panel(live, NEW) is None
