# Responsibility: Verify a reload keeps what the run view showed - the file's name, the run's clock
# and how long each stage took - read back from the server, not from the page's memory.
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
    assert durs["Mesh creation"] == "1:30" and durs["Mesh validation"] == "1:00", durs
    assert durs["Review"] == "0:50", durs
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
