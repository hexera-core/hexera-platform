# Responsibility: Verify the notes box on the brief card is one quiet line that grows on focus and as you type, with a styled Add button; Enter adds (Shift+Enter starts a line) and the notes read as a clean list.
# Boundaries: the console's own modules in the page; no network. It reads computed styles and the DOM, never pixels.
from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_the_notes_box_is_one_quiet_line_that_grows_and_enter_adds(live):
    out = live.evaluate("""(async () => {
      const { Stage } = await import('/static/js/render/stage.js');
      Stage.mount();
      const sent = [];
      const add = async (t) => { sent.push(t);
        Stage.brief({purpose:'structural', engine:'gmsh', user_notes:[{text:'Static stress'}, {text:t}]}, add); };
      Stage.brief({purpose:'structural', engine:'gmsh', user_notes:[{text:'Static stress'}]}, add);
      const box = () => document.querySelector('.rc-notes-in'), btn = () => document.querySelector('.rc-notes-add');
      const idle = {h: box().getBoundingClientRect().height, disabled: btn().disabled,
                    btnFont: getComputedStyle(btn()).fontFamily, btnBg: getComputedStyle(btn()).backgroundColor};
      box().focus(); box().value = 'Bolt holes matter';
      box().dispatchEvent(new Event('input'));
      await new Promise(r => setTimeout(r, 250));
      const focused = {h: box().getBoundingClientRect().height, disabled: btn().disabled};
      box().dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true}));
      await new Promise(r => setTimeout(r, 50));
      return {idle, focused, sent, notes: [...document.querySelectorAll('.rc-notes-list li.rc-note')].map(n => n.textContent)};
    })()""")
    assert out["idle"]["h"] <= 36 and out["idle"]["disabled"], out
    assert "Geist" in out["idle"]["btnFont"], "the Add button is the browser's own"
    assert out["focused"]["h"] >= 60 and not out["focused"]["disabled"], out
    assert out["sent"] == ["Bolt holes matter"]
    assert out["notes"] == ["Static stress", "Bolt holes matter"], out
