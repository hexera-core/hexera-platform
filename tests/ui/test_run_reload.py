# Responsibility: Verify a reload keeps what the run view showed - the file's name, the run's clock
# and how long each row took - read back from the server, not from the page's memory.
from __future__ import annotations

import pytest
from test_timeline_truth import run

pytestmark = pytest.mark.ui


def test_the_run_clock_is_the_servers_and_stops_when_the_job_ended(live):
    out = run(live, """
      const ago = (s) => new Date(Date.now() - s * 1000).toISOString();
      Stage.ensureProc();
      Stage.clock(ago(300));                       // a reload, five minutes into a run
      const running = read().clock;
      Stage.mount(); Stage.ensureProc();
      Stage.clock(ago(3600), ago(3600 - 125));     // a run that ended an hour ago, after 2:05
      await new Promise(r => setTimeout(r, 1200)); // the ticker must not move a stopped clock
      const ended = read().clock;
      Stage.mount(); Stage.ensureProc();
      play([{type:'stage', stage:'builder'}]);
      Stage.final({pass:true, attempts:1, text:'', createdAt: ago(600), endedAt: ago(10)});
      await new Promise(r => setTimeout(r, 1200));
      return {running, ended, final: read().clock};""")
    assert out["running"] in ("5:00", "5:01"), f"a reloaded run's clock read {out['running']}"
    assert out["ended"] == "2:05", f"a finished run's clock read {out['ended']}"
    assert out["final"] == "9:50", f"the clock did not stop at the job's end: {out['final']}"


def test_a_replayed_runs_stages_last_as_long_as_they_did(live):
    # A reload replays the log in an instant; every stage used to read 0:00.
    out = run(live, """
      const t0 = Date.now() - 3600 * 1000, at = (s) => new Date(t0 + s * 1000).toISOString();
      play([{type:'stage', stage:'builder', ts: at(0)},
            {type:'stage', stage:'executor', ts: at(90)},
            {type:'stage', stage:'reviewer', ts: at(150)},
            {type:'verdict', stage:'reviewer', verdict:'PASS', ts: at(195)}]);
      Stage.final({pass:true, attempts:1, text:'', createdAt: at(-5), endedAt: at(200)});
      return read();""")
    durs = {lane["name"]: lane["dur"] for lane in out["lanes"]}
    # the checks and the review are one row: one time, from the checks starting to the run ending
    assert durs["Designing the mesh"] == "1:30" and durs["Reviewing the mesh"] == "1:50", durs
    assert out["clock"] == "3:25"


def test_a_reloaded_run_names_its_file_from_the_server(live):
    out = live.evaluate("""(async () => {
      const C = await import('/static/js/shell/composer.js');
      const S = await import('/static/js/core/state.js');
      const label = () => document.getElementById('file-label').textContent;
      const before = S.get.sessionId();
      S.set.sessionId(null);
      C.showRunFile('aorta_fluid.stl');
      const reloaded = label();
      S.set.sessionId('sess-own-upload');            // a page that holds its own upload
      C.showRunFile('another.step');
      const own = label();
      C.showRunFile(null);
      S.set.sessionId(before);
      return {reloaded, own};
    })()""")
    assert out == {"reloaded": "aorta_fluid.stl", "own": "aorta_fluid.stl"}, out


def test_a_finished_job_carries_its_own_times(live):
    out = live.evaluate("""(async () => {
      const { terminalResult } = await import('/static/js/realtime/stream.js');
      return terminalResult({status:'failed', created_at:'2026-10-03T22:49:30Z',
                             ended_at:'2026-10-03T22:49:40Z'}, '');
    })()""")
    assert out["createdAt"] == "2026-10-03T22:49:30Z" and out["endedAt"] == "2026-10-03T22:49:40Z"


def test_a_step_is_never_longer_than_its_run_when_the_servers_clock_is_ahead(live):
    # Seen live (16 Nov): "Designing the mesh" read 22:23 while the run read 21:40. The run's clock counted from the
    # server's created_at, held at this browser's now while that was still "in the future", while each step counted
    # from when its event reached the page - two clocks a skew apart. The page now reads the server's clock throughout.
    out = live.evaluate("""(async () => {
      const { Stage } = await import('/static/js/render/stage.js');
      const { dispatch } = await import('/static/js/core/events.js');
      const realNow = Date.now;
      let now = realNow.call(Date);
      Date.now = () => now;
      try {
        const iso = (ms) => new Date(ms).toISOString();
        const B0 = now, AHEAD = 60000;                 // the server's clock is a minute ahead of this browser's
        Stage.mount(); Stage.newRun(); Stage.ensureProc();
        Stage.clock(iso(B0 + AHEAD));                  // the job was created just now, by the server's clock
        now = B0 + 10000;                              // ten seconds on, the design starts
        let c = dispatch({type: 'stage', stage: 'builder', ts: iso(B0 + AHEAD + 10000)}, Stage, null);
        c = dispatch({type: 'narrate', stage: 'builder', kind: 'say', text: 'Designing', ts: iso(B0 + AHEAD + 10000)}, Stage, c);
        now = B0 + 20000; Stage.clock(iso(B0 + AHEAD));     // the status polls
        now = B0 + 100000; Stage.clock(iso(B0 + AHEAD));
        await new Promise(r => setTimeout(r, 1200));         // the ticker draws
        const design = [...document.querySelectorAll('#tl .tl-node')].find(n => n.dataset.row === 'design');
        return {total: document.getElementById('proc-clock').textContent, design: design.querySelector('.dur').textContent};
      } finally { Date.now = realNow; }
    })()""")
    assert out == {"total": "1:40", "design": "1:30"}, out
