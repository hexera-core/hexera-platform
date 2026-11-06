# Responsibility: Verify a mesh run's live line in a real browser, fed through the one event dispatch
# a live or replayed run goes through: waiting for a worker, the engine and why, the plan, meshing
# with what the mesher says it is doing, checking quality, the AI review, and how the run ended -
# with the steps done ticked, a rebuild starting the steps again, and nothing moving once it ends.
# Boundaries: the console's own modules in the page; no network.
from __future__ import annotations

import pytest
from conftest import assert_clean

pytestmark = pytest.mark.ui

SETUP = """
  const { Stage } = await import('/static/js/render/stage.js');
  const { dispatch } = await import('/static/js/core/events.js');
  Stage.mount();
  let cur = null;
  const ev = (e) => { cur = dispatch(e, Stage, cur); };
  const line = () => { const g = document.querySelector('#run-progress .gp'); return {
    state: g.dataset.state, live: g.dataset.live === '1', now: g.querySelector('.gp-now').textContent,
    note: g.querySelector('.gp-note')?.textContent || '', advice: g.querySelector('.gp-advice')?.textContent || '',
    steps: [...g.querySelectorAll('.gp-step')].map(s => s.className.replace('gp-step ', '') + ':' + s.querySelector('.gp-label').textContent),
    inCard: !!document.querySelector('.proc #proc-live #run-progress') }; };
"""


def test_the_run_line_follows_the_run_step_by_step(live):
    out = live.evaluate("(async () => {" + SETUP + """
      Stage.ensureProc();
      Stage.waiting('Waiting for a worker - about 4 min while the fleet wakes up');
      const waiting = line();
      ev({type:'stage', stage:'engine_select'});
      ev({type:'note', stage:'engine_select', text:'Mesh engine: snappyHexMesh - it did best in our lab on shapes like this one', tone:'info'});
      ev({type:'stage', stage:'builder'});
      const planning = line();
      ev({type:'plan', stage:'builder', summary:'about 2.1M cells, 5 wall layers, finer cells in the wake'});
      ev({type:'meshing', stage:'builder', engine:'snappy', budget_s:1800, history:{typical_s:240, p10_s:200, p90_s:300, n:12}});
      ev({type:'mesh_stage', stage:'builder', key:'layers', label:'adding wall layers'});
      const meshing = line();
      ev({type:'meshed', stage:'builder', cells:2081224});
      ev({type:'stage', stage:'executor'});
      const checking = line();
      ev({type:'stage', stage:'reviewer'});
      const reviewing = line();
      ev({type:'verdict', stage:'reviewer', verdict:'PASS'});
      Stage.final({pass:true, createdAt:'2026-10-19T10:00:00Z', endedAt:'2026-10-19T10:07:12Z', attempts:1});
      return {waiting, planning, meshing, checking, reviewing, done: line()};
    })()""", timeout=60)
    assert out["waiting"]["inCard"] and out["waiting"]["live"] and out["waiting"]["now"] == "Waiting for a worker…"
    assert "fleet wakes up" in out["waiting"]["note"]
    assert out["planning"]["now"] == "Planning the mesh…"
    assert out["planning"]["steps"][0] == "done:Mesh engine: snappyHexMesh - it did best in our lab on shapes like this one"
    m = out["meshing"]
    assert m["now"] == "Meshing on the cloud mesher - adding wall layers…" and m["live"] is True
    assert "done:Plan: about 2.1M cells, 5 wall layers, finer cells in the wake" in m["steps"]
    assert out["checking"]["now"] == "Checking quality…"
    assert "done:Meshed - 2,081,224 cells" in out["checking"]["steps"]
    assert out["reviewing"]["now"] == "Reviewing the mesh against your brief…"
    d = out["done"]
    assert d["state"] == "done" and d["live"] is False and d["now"] == "Mesh ready in 7 min 12 s"
    assert "done:Review passed" in d["steps"] and all(s.startswith("done:") for s in d["steps"])
    assert_clean(live, "the run line through a whole run")


def test_a_rebuild_starts_the_steps_again_and_a_failed_run_stops_where_it_was(live):
    out = live.evaluate("(async () => {" + SETUP + """
      ev({type:'attempt', n:1});
      ev({type:'stage', stage:'builder'});
      ev({type:'meshing', stage:'builder', engine:'cfmesh', budget_s:600});
      ev({type:'meshed', stage:'builder', cells:900000});
      ev({type:'stage', stage:'reviewer'});
      ev({type:'verdict', stage:'reviewer', verdict:'FAIL'});
      ev({type:'attempt', n:2});
      ev({type:'stage', stage:'builder'});
      ev({type:'meshing', stage:'builder', engine:'cfmesh', budget_s:600});
      const second = line();
      Stage.final({pass:false, createdAt:'2026-10-19T10:00:00Z', endedAt:'2026-10-19T10:30:00Z'});
      return {second, ended: line()};
    })()""", timeout=60)
    s = out["second"]
    assert s["now"] == "Attempt 2: Meshing on the cloud mesher…"
    assert s["steps"][0] == "done:Mesh engine: cfmesh"            # the engine stands; nothing else does
    assert not any("Review" in x for x in s["steps"])
    e = out["ended"]
    assert e["state"] == "failed" and e["live"] is False
    assert e["now"] == "Stopped while meshing on the cloud mesher."
    assert "failed:Meshing on the cloud mesher" in e["steps"] and e["advice"]


def test_a_mesh_far_past_its_usual_time_says_so(live):
    out = live.evaluate("""(async () => {
      const { createRunProgress } = await import('/static/js/render/run_progress.js');
      let t = 0; const m = createRunProgress(() => t);
      m.lane('builder', 0);
      m.meshing('snappy', 1000, {typical_s: 240}, 0);
      t = 200e3; const usual = m.view().note;
      t = 300e3; const longer = m.view().note;
      t = 850e3; const near = m.view().note;
      t = 1100e3; const past = m.view().note;
      m.final({pass: false}, 1100e3);
      return {usual, longer, near, past, live: m.view().live};
    })()""", timeout=60)
    assert out["usual"] == ""
    assert out["longer"] == "Taking longer than similar runs (usually about 4 min)."
    assert out["near"] == "If it's still going at 16 min, it stops by itself."
    assert out["past"].startswith("Past its time limit") and out["live"] is False


def test_a_count_with_an_estimate_draws_a_bar_under_the_words_and_a_count_alone_draws_none(live):
    out = live.evaluate("(async () => {" + SETUP + """
      const bar = () => { const i = document.querySelector('#run-progress .gp-bar > i');
        return i ? {width: i.style.width, hidden: document.querySelector('#run-progress .gp-bar').getAttribute('aria-hidden')} : null; };
      ev({type:'stage', stage:'builder'});
      ev({type:'meshing', stage:'builder', engine:'cfmesh', budget_s:1800});
      ev({type:'narrate', stage:'builder', kind:'fill', text:'Filling the volume', n:1220000, unit:'cells'});
      const countOnly = {now: line().now, bar: bar()};
      ev({type:'narrate', stage:'builder', kind:'fill', text:'Filling the volume', n:1220000, of:1900000, unit:'cells'});
      const both = {now: line().now, bar: bar()};
      // a heartbeat says no numbers of its own: the bar holds where it was
      ev({type:'narrate', stage:'builder', kind:'fill', text:'Still filling the volume · 12 min'});
      const beating = {now: line().now, bar: bar()};
      ev({type:'meshed', stage:'builder', cells:1900000});
      return {countOnly, both, beating, afterMesh: bar()};
    })()""", timeout=60)
    assert out["countOnly"]["now"] == "Filling the volume · 1.2M cells so far…"
    assert out["countOnly"]["bar"] is None                      # an estimate nobody sent is not 0%
    assert out["both"]["now"] == "Filling the volume · 1.2M of about 1.9M cells…"
    assert out["both"]["bar"] == {"width": "64.2%", "hidden": "true"}
    assert out["beating"]["bar"]["width"] == "64.2%"            # a heartbeat moves nothing
    assert out["afterMesh"] is None                             # the mesh is done; no bar on a done line
    assert_clean(live, "the live line with a count and an estimate")


def test_the_bar_caps_at_full_and_never_walks_backwards(live):
    out = live.evaluate("""(async () => {
      const { createRunProgress } = await import('/static/js/render/run_progress.js');
      const { progressHtml } = await import('/static/js/render/check_progress.js');
      let t = 0; const m = createRunProgress(() => t);
      const say = (n, of) => m.narrate({kind:'fill', text:'Filling the volume', n, of, unit:'cells'}, t);
      m.lane('builder', 0);
      m.meshing('cfmesh', 1800, {}, 0);
      say(900000, 1900000);   const half = m.view().progress;
      say(820000, 1900000);   const reread = m.view();     // the mesher re-read its log: smaller
      say(2400000, 1900000);  const over = m.view();       // an `of` smaller than the count
      const width = (p) => (progressHtml(p, {}).match(/gp-bar[^]*?width:([^"]+)"/) || [])[1] || '';
      // a rebuild starts the estimate again from nothing
      m.attempt(2, t); m.lane('builder', t); m.meshing('cfmesh', 1800, {}, t);
      say(100000, 1900000);
      return {half, reread: reread.progress, over: over.progress, overWords: over.current,
              overWidth: width(over), rebuilt: m.view().progress};
    })()""", timeout=60)
    assert round(out["half"], 4) == 0.4737
    assert round(out["reread"], 4) == 0.4737                   # held, not walked back
    assert out["over"] == 1 and out["overWidth"] == "100.0%"   # capped, never past its track
    assert out["overWords"] == "Filling the volume · 2.4M of about 1.9M cells…"
    assert round(out["rebuilt"], 4) == 0.0526                  # a new mesh, a new estimate


def test_passing_the_estimate_holds_the_bar_at_full_and_the_next_mesh_starts_with_none(live):
    out = live.evaluate("(async () => {" + SETUP + """
      const bar = () => { const i = document.querySelector('#run-progress .gp-bar > i');
        return i ? i.style.width : null; };
      const say = (n, of) => ev({type:'narrate', stage:'builder', kind:'fill',
                                 text:'Filling the volume', n, of, unit:'cells'});
      ev({type:'stage', stage:'builder'});
      ev({type:'meshing', stage:'builder', engine:'cfmesh', budget_s:1800});
      say(1800000, 1900000);
      const nearly = {now: line().now, bar: bar()};
      // PAST THE ESTIMATE: contracts/mesh_progress stops sending `of`, so the words drop their
      // claim - but the bar holds at full rather than vanishing where a user is watching hardest
      say(2900000, undefined);
      const past = {now: line().now, bar: bar()};
      // a stage that counts nothing at all moves it no further and does not take it away
      ev({type:'narrate', stage:'builder', kind:'check', text:'Finishing up'});
      const quiet = {now: line().now, bar: bar()};
      // the rebuild after that overshoot starts empty: it has had no estimate of its own yet
      ev({type:'meshed', stage:'builder', cells:2900000});
      ev({type:'attempt', n:2});
      ev({type:'stage', stage:'builder'});
      ev({type:'meshing', stage:'builder', engine:'cfmesh', budget_s:1800});
      const rebuilt = {now: line().now, bar: bar()};
      say(300000, undefined);
      return {nearly, past, quiet, rebuilt, countOnlyRebuild: bar()};
    })()""", timeout=60)
    assert out["nearly"]["now"] == "Filling the volume · 1.8M of about 1.9M cells…"
    assert out["nearly"]["bar"] == "94.7%"
    assert out["past"]["now"] == "Filling the volume · 2.9M cells so far…"
    assert out["past"]["bar"] == "100%"                   # held at full, not taken away
    assert out["quiet"]["now"] == "Finishing up…" and out["quiet"]["bar"] == "100%"
    assert out["rebuilt"]["now"] == "Attempt 2: Meshing on the cloud mesher…"
    assert out["rebuilt"]["bar"] is None                  # a new mesh inherits no fraction
    assert out["countOnlyRebuild"] is None                # and a count alone still draws nothing
