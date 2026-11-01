# Responsibility: Verify the run shows its Repair check step: labelled "Repair check", between Engine selection and Mesh creation, its one line on the row in sight when folded, and the live line naming it while it runs.
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
    name: n.querySelector('.nm').textContent, said: n.querySelector('.ct').textContent,
    open: n.classList.contains('open'), mark: [...n.querySelector('.tl-dot').classList].filter(c => c !== 'tl-dot').join(' '),
    body: n.querySelector('.tl-inner').children.length }));
  Stage.ensureProc();
  ev({type:'stage', stage:'engine_select'});
  ev({type:'note', stage:'engine_select', text:'Mesh engine: cfMesh - the usual engine for this kind of run', tone:'info'});
  ev({type:'stage', stage:'repair'});
  const checking = document.querySelector('#run-progress .gp-now').textContent;
  ev({type:'note', stage:'repair', text:__LINE__, tone:'__TONE__'});
  ev({type:'stage', stage:'builder'});
  ev({type:'meshing', stage:'builder', engine:'cfmesh', budget_s:600});
  ev({type:'meshed', stage:'builder', cells:41200});
"""


def _play(live, line: str, tone: str = "info") -> dict:
    return live.evaluate("(async () => {" + PLAY.replace("__LINE__", json.dumps(line)).replace("__TONE__", tone) + """
      return {label: laneLabel('repair'), checking, rows: rows()};
    })()""", timeout=60)


def test_the_repair_check_sits_between_engine_selection_and_mesh_creation(live):
    out = _play(live, "Repaired 3 defects: closed 2 gaps, flipped 1 face")
    assert out["label"] == "Repair check"
    assert [r["name"] for r in out["rows"]] == ["Engine selection", "Repair check", "Mesh creation"], out
    repair = out["rows"][1]
    # its one line is the row's own words, in sight with the step folded - not hidden in the fold
    assert repair["said"] == "Repaired 3 defects: closed 2 gaps, flipped 1 face", repair
    assert not repair["open"] and repair["body"] == 0 and "done" in repair["mark"], repair
    # while it runs, the live line at the foot of the run says what it is doing
    assert out["checking"] == "Checking your part for repair…", out
    assert_clean(live, "the run with a repair check")


def test_defects_left_for_an_engineer_mark_the_step_with_a_warning(live):
    out = _play(live, "2 defects need an engineer's look; meshing your original file", "warn")
    repair = out["rows"][1]
    assert repair["said"].startswith("2 defects need an engineer"), repair
    assert "warn" in repair["mark"], repair
