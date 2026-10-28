# Responsibility: Verify in a real browser that Proceed waits for the geometry check on every route:
# while the served check says `checking`, the flow card's "Looks right, proceed", an FEA part's
# "Yes, proceed" and a thermal model's "Mesh this model" are off with "Waiting for the check to
# finish" beside them; once the check ends (done, failed or timed out) they are on again with
# their own words back - except an FEA part the full reading stopped, which keeps its own block.
# Boundaries: the console's own card renderers and gate, fed checks of the server's shape; the poll
# that calls the gate (shell/composer.js) is not driven here, and only the network is stood in for.
from __future__ import annotations

import json

import pytest
from conftest import assert_clean
from test_fea_stage_hot_spots import _fea_check
from test_geometry_stage_external import _check
from test_thermal_panel import served  # noqa: F401 - the thermal model's check, as the server serves it

pytestmark = pytest.mark.ui


def _gate(live, check: dict, button: str, hint: str) -> dict:
    return live.evaluate("""(async () => {
      const { gateProceed, WAIT_FOR_CHECK } = await import('/static/js/render/check_progress.js');
      const { Stage } = await import('/static/js/render/stage.js');
      window.__confirmed = 0;
      Stage.geometryCheck(__CHECK__, async () => { window.__confirmed += 1; return {message: 'ok'}; });
      const btn = () => document.querySelector('__BTN__');
      const hint = () => btn().parentElement.querySelector('__HINT__');
      const snap = () => ({disabled: btn().disabled, hint: hint().textContent, title: btn().title || ''});
      // the card alone: the page may still hold another test's stage
      const card = btn().closest('.im');
      const before = snap();
      const held = gateProceed(card, {checking: true});
      const waiting = snap();
      btn().click();                                 // a disabled button sends nothing
      await new Promise(r => setTimeout(r, 100));
      const sentWhileWaiting = window.__confirmed;
      gateProceed(card, {checking: true});           // the next poll, still at work: no change
      const again = snap();
      gateProceed(card, {checking: false});          // the check ended
      const after = snap();
      return {before, held, waiting, sentWhileWaiting, again, after, words: WAIT_FOR_CHECK};
    })()""".replace("__CHECK__", json.dumps(check)).replace("__BTN__", button).replace("__HINT__", hint))


def _assert_waits(out: dict) -> None:
    assert out["words"] == "Waiting for the check to finish"
    assert out["held"] == 1, out
    assert out["waiting"] == {"disabled": True, "hint": out["words"], "title": out["words"]}, out
    assert out["sentWhileWaiting"] == 0, out
    assert out["again"] == out["waiting"], out
    assert out["after"] == {**out["before"], "disabled": False, "title": ""}, out


def test_the_flow_cards_proceed_waits_for_the_check(live):
    check = _check(); check["skin"] = False
    _assert_waits(_gate(live, check, ".gc-card .gc-proceed", ".gc-hint"))
    assert_clean(live, "the flow card held for the check")


def test_an_fea_parts_proceed_waits_for_its_walls_and_hot_spots(live):
    _assert_waits(_gate(live, _fea_check([]), ".fea-card .gc-proceed", ".gc-hint"))
    assert_clean(live, "the FEA card held for the check")


def test_a_thermal_models_mesh_button_waits_for_the_check(live, served):  # noqa: F811
    check, _skin = served
    _assert_waits(_gate(live, check, ".tm-card .tm-mesh", ".tm-hint"))
    assert_clean(live, "the thermal card held for the check")


def test_an_fea_part_the_full_reading_stopped_keeps_its_own_block(live):
    check = _fea_check([])
    check["fea"]["single_part_only"] = "This file is a surface, not a closed solid."
    out = _gate(live, check, ".fea-card .gc-proceed", ".gc-hint")
    assert out["before"]["disabled"] is True and out["waiting"]["disabled"] is True
    assert out["after"]["disabled"] is True, out           # the gate never opens what was shut for a reason
