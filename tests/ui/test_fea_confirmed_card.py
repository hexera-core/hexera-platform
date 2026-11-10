# Responsibility: Verify an FEA part confirmed on the stage reads in the conversation as a short card - a title
# and a few lines - and never as the paragraph the intake reads.
# Boundaries: the declaration is built by the server's own code; the shipped modules render it in a real browser.
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from conftest import assert_clean

from meshpipeline.application.geometry_confirmation import fea_confirmation_message

pytestmark = pytest.mark.ui


def _declaration(**kw) -> str:
    body = {"part": "Helmet Model for FEA Simulation", "bodies": 3, "fea_connection": "bonded",
            "parts_summary": "3 parts: Part_1, Part_2 and Part_3",
            "joints_summary": "2 joints: 2 places where parts touch",
            "clean_summary": "The geometry is clean. 2 pair(s) of bodies touch without overlapping",
            "size_mm": [235.0, 329.0, 252.0], "unit": "mm", "scale_to_m": 0.001}
    body.update(kw)
    return fea_confirmation_message(SimpleNamespace(**body))


def _render(live, text: str) -> dict:
    js = """(async () => {
      const { Stage } = await import('/static/js/render/stage.js');
      Stage.chat('assistant', __T__);
      const all = [...document.querySelectorAll('#cc .im.assistant .txt')];
      const t = all[all.length - 1];
      return {text: t.textContent, items: [...t.querySelectorAll('li')].map((li) => li.textContent.replace(/\\s+/g, ' ').trim()),
              bold: (t.querySelector('strong') || {}).textContent || ''};
    })()""".replace("__T__", json.dumps(text))
    return live.evaluate(js, timeout=60)


def test_an_fea_assembly_confirmation_is_a_short_card(live):
    out = _render(live, _declaration())
    assert out["bold"] == "✓ Confirmed: Helmet Model for FEA Simulation"
    assert out["items"][:4] == ["FEA · solid mesh", "235 × 329 × 252 mm", "3 parts · bonded · 2 joints", "✓ Geometry clean"]
    assert "input_kind" not in out["text"] and "(Unsaved)" not in out["text"] and len(out["text"]) < 260
    assert_clean(live, "an FEA confirmation card")


def test_a_shell_in_inches_that_is_not_clean_says_so(live):
    out = _render(live, _declaration(bodies=1, fea_connection="", unit="in", scale_to_m=0.0254,
                                     clean_summary="Part_1 may have self-crossing faces",
                                     mesh_as_summary="Meshed as: a shell - the mid-surface with plate elements, each "
                                                     "with its wall thickness, in Nastran, CalculiX, Abaqus and Ansys files."))
    assert out["items"][0] == "FEA · shell (mid-surface, plate elements) mesh"
    assert out["items"][1].startswith("235 × 329 × 252 mm (file in inches")
    assert not any("parts" in i for i in out["items"]) and "⚠ Not clean - we'll still try" in out["items"]
