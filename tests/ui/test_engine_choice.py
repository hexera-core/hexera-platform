# Responsibility: Verify the engine choice under the one engine question shows every engine that can take
# the file with its measured fit, marks the recommendation without picking it, and sends the user's pick.
# Boundaries: the payload is the one the chat route returns (agents/intake/measured_choice.choice_payload);
# only the network is stood in for.
from __future__ import annotations

import json

import pytest
from tests.ui.conftest import assert_clean

pytestmark = pytest.mark.ui

SESSION = "6a1d6a3e-5c1e-4a8e-9b7e-2f0c8d1e0c11"

CHOICE = {
    "proposed": "snappy", "recommended": "snappy",
    "shape": "a thin gap or annulus", "table_version": "2026-10-05-test",
    "engines": [
        {"engine": "snappy", "label": "snappyHexMesh", "fit": "best fit", "recommended": True,
         "outside": False, "layers": True, "proposed": True, "reply": "Use snappyHexMesh",
         "reason": "passed 6 of 6 similar shapes (thin gaps or annuli, from CAD); near-wall "
                   "layers on 96% of the wall; about 5 minutes a mesh",
         "evidence": {"runs": 6, "passed": 6, "shapes": 6}},
        {"engine": "cfmesh", "label": "cfMesh", "fit": "poor fit", "recommended": False,
         "outside": False, "layers": True, "proposed": False, "reply": "Use cfMesh",
         "reason": "passed 1 of 6 similar shapes (thin gaps or annuli, from CAD)",
         "heads_up": "The narrowest passage gets fewer than 10 cells across within the cell budget.",
         "evidence": {"runs": 6, "passed": 1, "shapes": 6}},
        {"engine": "gmsh", "label": "Gmsh", "fit": "fair fit", "recommended": False,
         "outside": False, "layers": False, "proposed": False, "reply": "Use Gmsh",
         "reason": "passed 3 of 6 similar shapes (thin gaps or annuli, from CAD); no reliable "
                   "near-wall layers",
         "evidence": {"runs": 6, "passed": 3, "shapes": 6}},
    ],
}


def test_every_engine_is_listed_with_its_fit_and_only_the_recommendation_is_marked(live):
    seen = live.evaluate("""(async () => {
      const { Stage } = await import('/static/js/render/stage.js');
      window.__picked = null;
      Stage.engineChoice(__CHOICE__, (reply) => { window.__picked = reply; });
      const rows = [...document.querySelectorAll('.eng-choice .ec-row')];
      return rows.map(r => ({engine: r.dataset.engine, rec: r.classList.contains('rec'),
                             badge: !!r.querySelector('.ec-badge'),
                             fit: r.querySelector('.ec-fit').textContent,
                             why: r.querySelector('.ec-why').textContent,
                             button: r.querySelector('.ec-pick').textContent,
                             disabled: r.querySelector('.ec-pick').disabled}));
    })()""".replace("__CHOICE__", json.dumps(CHOICE)))
    assert [r["engine"] for r in seen] == ["snappy", "cfmesh", "gmsh"], seen
    assert [r["rec"] for r in seen] == [True, False, False]
    assert [r["badge"] for r in seen] == [True, False, False]
    assert seen[1]["fit"] == "poor fit" and "passed 1 of 6 similar shapes" in seen[1]["why"]
    notes = live.evaluate("[...document.querySelectorAll('.eng-choice .ec-row')].map("
                          "r => (r.querySelector('.ec-note') || {}).textContent || '')")
    assert notes == ["", "Heads-up: The narrowest passage gets fewer than 10 cells across within the "
                         "cell budget.", ""], notes
    assert "no reliable near-wall layers" in seen[2]["why"]
    assert [r["button"] for r in seen] == ["Use snappyHexMesh", "Use cfMesh", "Use Gmsh"]
    # nothing is chosen for the user: every engine can still be picked
    assert not any(r["disabled"] for r in seen)
    assert live.evaluate("window.__picked") is None
    shape = live.evaluate("document.querySelector('.eng-choice .ec-shape').textContent")
    assert shape == "This looks like a thin gap or annulus."
    assert_clean(live, "the engine choice")


def test_the_users_pick_is_sent_in_their_words_and_the_card_is_retired(live):
    live.evaluate("""(async () => {
      const { Stage } = await import('/static/js/render/stage.js');
      window.__picked = null;
      Stage.engineChoice(__CHOICE__, (reply) => { window.__picked = reply; });
      document.querySelector('.eng-choice .ec-row[data-engine="cfmesh"] .ec-pick').click();
    })()""".replace("__CHOICE__", json.dumps(CHOICE)))
    assert live.evaluate("window.__picked") == "Use cfMesh"
    state = live.evaluate("""(() => ({
      disabled: [...document.querySelectorAll('.eng-choice .ec-pick')].every(b => b.disabled),
      picked: [...document.querySelectorAll('.eng-choice .ec-row.picked')].map(r => r.dataset.engine),
      done: document.querySelector('.eng-choice').classList.contains('done')}))()""")
    assert state == {"disabled": True, "picked": ["cfmesh"], "done": True}, state


def test_a_reply_without_a_choice_retires_the_open_card(live):
    left = live.evaluate("""(async () => {
      const { Stage } = await import('/static/js/render/stage.js');
      Stage.engineChoice(__CHOICE__, () => {});
      Stage.engineChoice(null, () => {});
      return {cards: document.querySelectorAll('.eng-choice').length,
              open: [...document.querySelectorAll('.eng-choice .ec-pick')].filter(b => !b.disabled).length};
    })()""".replace("__CHOICE__", json.dumps(CHOICE)))
    assert left == {"cards": 1, "open": 0}, left


def test_the_composer_draws_the_choice_from_the_reply_and_sends_the_pick_as_a_message(live):
    # The whole loop through the real composer: the chat route's reply carries engine_choice, the
    # card is drawn under the question, and a click posts the user's own words as the next message.
    live.evaluate("""(async () => {
      const CHOICE = __CHOICE__;
      window.__bodies = [];
      const real = window.fetch.bind(window);
      window.fetch = (u, o) => {
        if (String(u).includes('/api/v1/chat/message')) {
          const body = JSON.parse(o.body); window.__bodies.push(body.content);
          const first = window.__bodies.length === 1;
          const reply = first
            ? {session_id: body.session_id, reply: 'This looks like a thin gap or annulus. ' +
               "I'd mesh it with snappyHexMesh. OK, or pick another engine?", engine_choice: CHOICE}
            : {session_id: body.session_id, reply: 'cfMesh it is.', engine_choice: null};
          return Promise.resolve(new Response(JSON.stringify(reply),
            {status: 200, headers: {'Content-Type': 'application/json'}}));
        }
        return real(u, o);
      };
      const st = await import('/static/js/core/state.js');
      st.set.sessionId('__S__');
      const input = document.getElementById('chat-input');
      input.disabled = false; input.value = 'internal flow through the annulus';
      document.getElementById('send-btn').disabled = false;
      document.getElementById('send-btn').click();
    })()""".replace("__CHOICE__", json.dumps(CHOICE)).replace("__S__", SESSION))
    live.wait_for("document.querySelectorAll('.eng-choice .ec-pick').length === 3", timeout=30,
                  what="the engine choice to be drawn under the question")
    live.evaluate("document.querySelector('.eng-choice .ec-row[data-engine=\"gmsh\"] .ec-pick').click()")
    live.wait_for("window.__bodies.length === 2", timeout=30, what="the pick to be sent")
    assert live.evaluate("window.__bodies") == ["internal flow through the annulus", "Use Gmsh"]
    live.wait_for("[...document.querySelectorAll('.eng-choice .ec-pick')].every(b => b.disabled)",
                  timeout=30, what="the answered question's card to be retired")
