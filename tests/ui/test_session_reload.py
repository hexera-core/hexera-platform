# Responsibility: Verify a reload in the middle of the questionnaire draws the stored conversation on the one drawing path, with the open step's buttons and the plan card, and leaves the box open on that step.
# Boundaries: the console's own modules in the page; the history is handed in as the server serves it. It reads the DOM.
from __future__ import annotations

import json

import pytest
from conftest import assert_clean

pytestmark = pytest.mark.ui

HISTORY = {"session_id": "s-reload", "job_id": None, "geometry_filename": "bracket.step",
           "messages": [{"role": "assistant", "content": "Geometry received. What are you meshing this for?"},
                        {"role": "user", "content": "FEA"},
                        {"role": "assistant", "content": "What will you analyze?"},
                        {"role": "user", "content": "x", "_synthetic": True}],
           "choices": [{"label": "Static stress", "send": "Static stress", "soon": False}],
           "brief": None}


def test_a_reload_draws_the_conversation_and_its_open_step(live):
    out = live.evaluate("""(async () => {
      const { intakeOrder } = await import('/static/js/core/conversation.js');
      const C = await import('/static/js/shell/composer.js');
      const { Stage } = await import('/static/js/render/stage.js');
      Stage.mount();
      const h = __H__;
      for (const item of intakeOrder(h.messages)) Stage.chat(item.role, item.text);
      C.resumeSession(h.session_id, h, { watch: false });
      return {said: [...document.querySelectorAll('#cc .im .txt, #cc .im .bub')].map(t => t.textContent.trim()),
              buttons: [...document.querySelectorAll('.qc-btn')].map(b => b.textContent),
              label: document.getElementById('file-label').textContent,
              open: !document.getElementById('chat-input').disabled,
              upload: !!document.getElementById('upload-btn')};
    })()""".replace("__H__", json.dumps(HISTORY)))
    assert out["said"] == ["Geometry received. What are you meshing this for?", "FEA", "What will you analyze?"], out
    assert out["buttons"] == ["Static stress"] and out["label"] == "bracket.step", out
    assert out["open"] and not out["upload"], out
    assert_clean(live, "a reloaded conversation")
