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


def test_a_case_refused_before_the_mesher_does_not_tick_meshed_or_blame_the_quality_check(live):
    """THE EXECUTOR LANE OPENS TWICE and means a different thing each time.

    Its first pass is a PRE-FLIGHT that runs before any mesher and may refuse the case outright;
    its second reads the finished mesh. Both arrive as `stage: executor`, and both used to start
    the "Checking quality" step - so a customer cold plate refused on the mesher's octree ceiling
    (job 82e77c58, a real run) was shown "Stopped while checking quality." under a ticked
    "Meshed", `start` having swept the un-run mesh step to done on its way past. The engine's own
    message rendered directly below said the opposite, in plain words: "no mesher was started, so
    no time was spent on a mesh".

    The sequence below is that run's, event for event: an engine is chosen, the builder opens and
    prices the mesh, no MESHING event is ever sent because no mesher is ever started, the
    executor's pre-flight refuses, and the run ends without a mesh.
    """
    out = live.evaluate("(async () => {" + SETUP + """
      Stage.ensureProc();
      ev({type:'stage', stage:'engine_select'});
      ev({type:'note', stage:'engine_select', text:'Mesh engine: cfMesh conjugate - it did best in our lab on shapes like this one', tone:'info'});
      ev({type:'stage', stage:'builder'});
      ev({type:'stage', stage:'executor'});          // the PRE-FLIGHT, with no mesher behind it
      const refused = line();
      Stage.final({pass:false, createdAt:'2026-11-13T09:25:00Z', endedAt:'2026-11-13T09:39:56Z'});
      return {refused, ended: line()};
    })()""", timeout=60)
    e = out["ended"]
    assert e["state"] == "failed" and e["live"] is False
    # NOT "Stopped while checking quality." - nothing was checked, because nothing was meshed.
    assert e["now"] == "Stopped while planning the mesh.", e["now"]
    assert "failed:Planning the mesh" in e["steps"], e["steps"]
    # and no tick anywhere claiming a mesh that does not exist
    assert not any(s.startswith("done:") and "Mesh" in s and "engine" not in s.lower()
                   for s in e["steps"]), e["steps"]
    assert not any("Checking quality" in s or "Checked quality" in s
                   for s in e["steps"] if s.startswith(("done:", "failed:"))), e["steps"]
    assert_clean(live, "a case refused before the mesher")


def test_the_quality_step_still_opens_once_a_mesher_has_run(live):
    """THE CONTROL for the row above, so the fix cannot be "never open the quality step".

    Same executor lane, same event type - the one difference is that a mesher ran first. This is
    what every delivered run does, and it must read exactly as it always has.
    """
    out = live.evaluate("(async () => {" + SETUP + """
      Stage.ensureProc();
      ev({type:'stage', stage:'builder'});
      ev({type:'meshing', stage:'builder', engine:'cfmesh', budget_s:600});
      ev({type:'meshed', stage:'builder', cells:2081224});
      ev({type:'stage', stage:'executor'});
      return {checking: line()};
    })()""", timeout=60)
    c = out["checking"]
    assert c["now"] == "Checking quality…", c["now"]
    assert "done:Meshed - 2,081,224 cells" in c["steps"], c["steps"]
    assert_clean(live, "the quality step after a real mesh")
