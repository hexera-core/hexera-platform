# Responsibility: Verify the run shows its repair check as one line of the "Choosing the engine" row, in sight with the row's steps folded, inside the run card, and the live line naming it while it runs.
# Boundaries: the console's own modules in the page, fed through the one event dispatch; no network. It reads the DOM, never pixels.
from __future__ import annotations

import json

import pytest
from conftest import assert_clean

pytestmark = pytest.mark.ui

PLAY = """
  const { Stage } = await import('/static/js/render/stage.js');
  const { dispatch, laneLabel } = await import('/static/js/core/events.js');
  Stage.mount();
  let cur = null;
  const ev = (e) => { cur = dispatch(e, Stage, cur); };
  const rows = () => [...document.querySelectorAll('#tl .tl-node')].map(n => ({
    name: n.querySelector('.nm').textContent,
    said: [...n.querySelectorAll('.rr-said .rr-line')].map(p => p.textContent),
    open: n.classList.contains('open'), mark: [...n.querySelector('.tl-dot').classList].filter(c => c !== 'tl-dot').join(' ') }));
  Stage.ensureProc();
  ev({type:'stage', stage:'engine_select'});
  ev({type:'note', stage:'engine_select', text:'Mesh engine: cfMesh - the usual engine for this kind of run', tone:'info'});
  ev({type:'stage', stage:'repair'});
  const checking = [...document.querySelectorAll('.rr-live')].find(e => !e.hidden).querySelector('.rr-now').textContent;
  ev({type:'note', stage:'repair', text:__LINE__, tone:'__TONE__'});
  ev({type:'stage', stage:'builder'});
  ev({type:'meshing', stage:'builder', engine:'cfmesh', budget_s:600});
  ev({type:'meshed', stage:'builder', cells:41200});
"""


def _play(live, line: str, tone: str = "info") -> dict:
    return live.evaluate("(async () => {" + PLAY.replace("__LINE__", json.dumps(line)).replace("__TONE__", tone) + """
      return {label: laneLabel('repair'), checking, rows: rows(),
              inCard: !!document.querySelector('.proc .proc-head .proc-title') && !!document.querySelector('.proc #tl .tl-node')};
    })()""", timeout=60)


def test_the_repair_check_is_a_line_of_the_engine_row(live):
    out = _play(live, "Repaired 3 defects: closed 2 gaps, flipped 1 face")
    assert out["label"] == "Choosing the engine"
    assert [r["name"] for r in out["rows"]] == ["Choosing the engine", "Designing the mesh",
                                                "Creating the mesh", "Reviewing the mesh"], out
    # the rows sit inside the run's own card, under its "Generating mesh" header
    assert out["inCard"], out
    engine = out["rows"][0]
    # the engine and the repair check, each one line in sight with the row folded
    assert engine["said"] == ["cfMesh - the usual engine for this kind of run",
                              "Repaired 3 defects: closed 2 gaps, flipped 1 face"], engine
    assert not engine["open"] and "done" in engine["mark"], engine
    # while it runs, the live line in the row says what it is doing
    assert out["checking"] == "Checking your part for repair…", out
    assert_clean(live, "the run with a repair check")


def test_defects_left_for_an_engineer_mark_the_row_with_a_warning(live):
    out = _play(live, "2 defects need an engineer's look; meshing your original file", "warn")
    engine = out["rows"][0]
    assert engine["said"][1].startswith("2 defects need an engineer"), engine
    assert "warn" in engine["mark"], engine
