# Responsibility: Verify the plan step's one Proceed - at the right end of the plan bar pinned above the chat box, not on the card - says Proceed once, and that the stage's Proceed never draws the application's own turn as the user's words.
# Boundaries: the console's own modules in the page; the confirm endpoint is stood in for in the page. It reads the DOM, never pixels.
from __future__ import annotations

import pytest
from conftest import assert_clean

pytestmark = pytest.mark.ui

PLAN = {"purpose": "structural", "engine": "gmsh", "user_notes": [],
        "proceed_ask": "Here's the plan. Tell me any mesh requirements here, then say go or press Proceed."}


def test_the_plan_bar_has_proceed_at_its_right_end_and_a_press_says_proceed_once(live):
    out = live.evaluate("""(async () => {
      const { Stage } = await import('/static/js/render/stage.js');
      Stage.mount();
      const said = [];
      Stage.brief(__PLAN__, async () => {}, null, () => said.push('Proceed'));
      const card = document.querySelector('.req-card');
      // NOTE-18: no notes box and no Proceed on the card; Proceed is the plan bar's, at its right end, above the box
      const go = document.querySelector('#plan-bar .rc-go'), line = go.parentElement;
      const after = !card.querySelector('.rc-notes-box, textarea, .rc-go') && line.lastElementChild === go
        && document.getElementById('input-bar').firstElementChild === document.getElementById('plan-bar');
      go.click(); go.click();
      // a plan no longer awaiting approval has no button
      Stage.brief({...__PLAN__, proceed_ask: undefined, engine: 'netgen'}, async () => {}, null, () => said.push('x'));
      return {label: go.textContent, after, said, gone: !document.querySelector('.rc-go')};
    })()""".replace("__PLAN__", __import__("json").dumps(PLAN)))
    assert out["label"] == "Proceed" and out["after"], out
    assert out["said"] == ["Proceed"], "one press, one Proceed"
    assert out["gone"], out
    assert_clean(live, "the plan card")


def test_the_stages_proceed_never_draws_the_applications_turn_as_the_users(live):
    from meshpipeline.application.geometry_hold import CONTINUE_TEXT
    out = live.evaluate("""(async () => {
      const { Stage } = await import('/static/js/render/stage.js');
      Stage.mount();
      // what composer.confirmFn does with the confirm's answer
      const reply = {message: 'GEOMETRY CHECK (confirmed by the user): x', continued_with: __CONT__,
                     next: {reply: 'What will you analyze?', choices: []}};
      const src = await (await fetch('/static/js/shell/composer.js')).text();
      return {draws: /deps\\.chat\\(\\s*"user"\\s*,\\s*reply\\.continued_with/.test(src), seen: reply.continued_with};
    })()""".replace("__CONT__", __import__("json").dumps(CONTINUE_TEXT)))
    assert out["seen"] == CONTINUE_TEXT and not out["draws"]
