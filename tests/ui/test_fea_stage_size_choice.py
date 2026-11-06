# Responsibility: Verify PLAN-fea-22's size choice on an FEA part's stage: while the check sizes the mesh the panel says so
# and Proceed stays on; once sized with a choice, the full-rules and the quicker mesh are shown with their size, machine
# and time, the recommended one preselected and marked, the quicker one's shortfalls listed; the pick the user makes is
# the one the confirmation carries; with nothing to choose there is no radio, only the size.
# Boundaries: the skin is built through the worker's own code from a tube made here; the FEA view is the server's own
# (application/fea_check.plan_view) over a plan record written here. Only the network is stood in for.
from __future__ import annotations

import pytest
from conftest import assert_clean
from test_geometry_stage_open_ends import R_IN, R_OUT, _open, _skin, _tube

pytestmark = pytest.mark.ui

FULL = {"elements": 5_400_000, "standard": False, "memory_gib": 64, "minutes": 82, "usual_minutes": 40}
QUICKER = {"elements": 3_380_000, "standard": True, "minutes": 52, "usual_minutes": 25, "short_zones": 19,
           "short": ["19 rounds kept for their shape, coarsened"], "short_kinds": ["round"]}


def _check(plan_fact: dict) -> dict:
    from meshpipeline.application import fea_check

    plan = fea_check.plan_view({fea_check.PLAN_FACT_KEY: plan_fact})
    return {"status": "ready", "named": True, "skin": True, "pictures": [], "proposal": {
        "part": "extrusion", "input_kind": "body-surface", "size_mm": [150.0, 10.0, 10.0], "unit": "mm",
        "unit_basis": "file", "notes": [], "openings": [], "faces": [], "holes": []},
        "fea": {"question": "Is this your part and are the units right?", "bodies": 1, "clean": True,
                "clean_summary": "watertight", "single_part_only": "", "can_proceed": True,
                "measure": {"measured": True, "basis": "cad", "thickness_min_mm": 0.25, "thickness_face": "Face_1",
                            "hot_spots": [], "counts": {}, "more": 0, "summary": "", "notes": []},
                **({"plan": plan} if plan else {})}}


def _plan(live, sid: str) -> dict:
    return live.evaluate(f"""(() => {{
      const root = document.getElementById('gstage-{sid}');
      const box = root.querySelector('.fea-plan');
      const radios = [...root.querySelectorAll('input[name="fea-plan"]')];
      const proceed = root.querySelector('.gc-proceed');
      return {{box: !!box, text: box ? box.textContent.replace(/\\s+/g, ' ').trim() : '',
               radios: radios.map((r) => [r.value, r.checked]),
               rec: [...root.querySelectorAll('.fea-plan-opt')].filter((o) => o.querySelector('.fea-plan-rec'))
                      .map((o) => o.querySelector('input').value),
               short: [...root.querySelectorAll('.fea-plan-short li')].map((li) => li.textContent.trim()),
               proceed: proceed ? !proceed.disabled : null}};
    }})()""")


def test_the_quicker_mesh_is_offered_recommended_and_the_pick_is_confirmed(live):
    sid = "feasize"
    _open(live, sid, _skin(_tube(R_OUT, R_IN)), _check({
        "sized": True, "choice": True, "recommended": "quicker", "full": FULL, "quicker": QUICKER,
        "why": "The quicker mesh coarsens only rounds kept for their shape."}))
    p = _plan(live, sid)
    assert p["box"] and p["radios"] == [["full", False], ["quicker", True]]
    assert p["rec"] == ["quicker"]
    assert "5.4 million elements" in p["text"] and "a 64 GiB machine" in p["text"]
    assert "3.4 million elements" in p["text"] and "the standard machine" in p["text"]
    assert "19 zones coarser than their rules" in p["text"] and "A retry can take longer." in p["text"]
    assert p["short"] == ["19 rounds kept for their shape, coarsened"]
    assert p["proceed"] is True
    # the user takes the full-rules mesh instead: the confirmation carries their pick
    live.evaluate(f"""(() => {{ const r = document.querySelector('#gstage-{sid} input[name="fea-plan"][value="full"]');
      r.checked = true; r.dispatchEvent(new Event('change', {{bubbles: true}})); }})()""")
    live.evaluate(f"document.querySelector('#gstage-{sid} .gc-proceed').click()")
    live.wait_for(f"window.__confirmed['{sid}']", timeout=30, what="the confirmation to be sent")
    assert live.evaluate(f"window.__confirmed['{sid}']")["fea_plan"] == "full"
    assert_clean(live, "an FEA part's size choice")


def test_while_sizing_the_panel_says_so_and_proceed_stays_on(live):
    sid = "feasizing"
    _open(live, sid, _skin(_tube(R_OUT, R_IN)), _check({"sizing": True}))
    p = _plan(live, sid)
    assert p["box"] and p["radios"] == [] and "Sizing the mesh" in p["text"]
    assert p["proceed"] is True
    live.evaluate(f"document.querySelector('#gstage-{sid} .gc-proceed').click()")
    live.wait_for(f"window.__confirmed['{sid}']", timeout=30, what="the confirmation to be sent")
    assert "fea_plan" not in live.evaluate(f"window.__confirmed['{sid}']")
    assert_clean(live, "an FEA part being sized")


def test_nothing_to_choose_shows_the_size_only(live):
    sid = "feafits"
    _open(live, sid, _skin(_tube(R_OUT, R_IN)), _check({
        "sized": True, "choice": False, "recommended": "full", "why": "",
        "full": {"elements": 180_000, "standard": True, "minutes": 6, "usual_minutes": 3}}))
    p = _plan(live, sid)
    assert p["box"] and p["radios"] == []
    assert "about 180,000 elements" in p["text"] and "the standard machine" in p["text"]
    assert_clean(live, "an FEA part with nothing to choose")
