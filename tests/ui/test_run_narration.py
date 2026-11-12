# Responsibility: Verify a run's narration in a real browser: the decisions Hexera says as it designs stay in view on the design row, the machine and the mesher's stages read in plain words with their counts on the live line of the row running, a heartbeat adds its note without losing the count, and the mesher's own label never reaches the page once the run narrates.
# Boundaries: the console's own modules in the page, fed through the one event dispatch; no network.
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
  const line = () => { const l = [...document.querySelectorAll('.proc .rr-live')].find(e => !e.hidden);
    return { now: l ? l.querySelector('.rr-now').textContent : '', note: l?.querySelector('.rr-note')?.textContent || '',
    row: l ? l.closest('.tl-node').dataset.row : '', live: !!l,
    said: [...document.querySelectorAll('[data-row=design] .rr-dec li')].map(p => p.textContent),
    text: document.querySelector('.proc').textContent }; };
"""


def test_the_run_says_what_it_decided_and_what_it_is_doing(live):
    out = live.evaluate("(async () => {" + SETUP + """
      Stage.ensureProc();
      ev({type:'stage', stage:'builder'});
      ev({type:'narrate', stage:'builder', kind:'say', text:'The 26 fillets and 3 holes carry the stress, so they get the finest cells.'});
      ev({type:'narrate', stage:'builder', kind:'say', text:'The 26 fillets and 3 holes carry the stress, so they get the finest cells.'});
      const planning = line();
      ev({type:'narrate', stage:'builder', kind:'machine', text:'Meshing on a large machine (256 GB)'});
      ev({type:'meshing', stage:'builder', engine:'gmsh', budget_s:7200});
      const machine = line();
      ev({type:'mesh_stage', stage:'builder', key:'volume', label:'filling the solid with elements (snappyHexMesh)'});
      ev({type:'narrate', stage:'builder', kind:'fill', text:'Filling the volume', n:1230000, of:1900000, unit:'cells'});
      const filling = line();
      ev({type:'narrate', stage:'builder', kind:'fill', text:'Still filling the volume · 34 min'});
      const beat = line();
      ev({type:'meshed', stage:'builder', cells:1874815});
      Stage.final({pass:true, createdAt:'2026-10-28T10:00:00Z', endedAt:'2026-10-28T10:40:00Z', attempts:1});
      return {planning, machine, filling, beat, done: line()};
    })()""", timeout=60)
    assert out["planning"]["said"] == ["The 26 fillets and 3 holes carry the stress, so they get the finest cells."]
    assert out["planning"]["row"] == "design"
    assert out["machine"]["now"] == "Meshing on a large machine (256 GB)…"
    f = out["filling"]
    assert f["now"] == "Filling the volume · 1.2M of about 1.9M cells…" and f["row"] == "build"
    assert "snappy" not in f["text"].lower()
    b = out["beat"]
    assert b["now"] == f["now"] and b["note"].startswith("Still filling the volume · 34 min")
    d = out["done"]
    assert not d["live"] and d["said"] == out["planning"]["said"]
    assert_clean(live, "a narrated run")


def test_a_run_that_does_not_narrate_still_shows_the_mesher_stage(live):
    out = live.evaluate("(async () => {" + SETUP + """
      Stage.ensureProc();
      ev({type:'stage', stage:'builder'});
      ev({type:'meshing', stage:'builder', engine:'snappy', budget_s:1800});
      ev({type:'mesh_stage', stage:'builder', key:'layers', label:'adding wall layers'});
      return line();
    })()""", timeout=60)
    assert out["now"] == "Meshing on the cloud mesher - adding wall layers…"
    assert out["said"] == []


def test_work_before_the_mesh_is_what_the_running_step_says(live):
    out = live.evaluate("(async () => {" + SETUP + """
      Stage.ensureProc();
      ev({type:'stage', stage:'builder'});
      ev({type:'narrate', stage:'builder', kind:'measure', text:'Measuring your part'});
      const measuring = line();
      ev({type:'narrate', stage:'builder', kind:'plan', text:'Planning the mesh · Pass 2 of up to 6'});
      const planning = line();
      ev({type:'meshing', stage:'builder', engine:'gmsh', budget_s:3600});
      return {measuring, planning, meshing: line()};
    })()""", timeout=60)
    assert out["measuring"]["now"] == "Measuring your part…" and out["measuring"]["row"] == "design"
    assert out["planning"]["now"] == "Planning the mesh · Pass 2 of up to 6…"
    assert out["meshing"]["now"] != out["planning"]["now"] and out["meshing"]["row"] == "build"
