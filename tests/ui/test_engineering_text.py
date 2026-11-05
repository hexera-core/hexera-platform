# Responsibility: Verify the console shows engineering text exactly as the server makes it, keeps numbers with their units, and leaves code and the user's words alone.
# Boundaries: ui/js/core/engineering_text.js and the chat bubble that uses it, in a real browser; the rules themselves are pinned in tests/unit/contracts.
from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from meshpipeline.contracts.engineering_text import plain

pytestmark = pytest.mark.ui

REPO = Path(__file__).parents[2]
CASES = json.loads((REPO / "tests" / "fixtures" / "engineering_text_cases.json")
                   .read_text(encoding="utf-8"))["cases"]
MODULE = "/static/js/core/engineering_text.js"
NBSP, NNBSP = "\u00a0", "\u202f"

# Pieces of what a model writes, recombined at random: the mirror must agree with the server on
# text nobody wrote a case for, not only on the cases.
_TOKENS = ["\\(", "\\)", "\\[", "\\]", "$", "$$", "^", "_", "{", "}", "\\text{", "\\mathrm{",
           "\\omega", "\\approx", "\\times", "\\,", "\\!", "\\ ", "\\\\", "\\frac", "\\sqrt", "\\bar",
           "\\circ", "\\sin", "\\Delta", "\\infty", "\\SI", "\\foo", "`", "y+", "y", "+", "-", "–",
           "=", " ", "\n", "0", "1", "30", "300", "2.5", "10", "x", "k", "C", "D", "m", "s", "/", ",",
           ".", "(", ")", "inlet_1", "wing.step", "C:\\a\\b", "/home/a/b", "http://x.y/z", "NACA ",
           "-omega", "°", "μ", "⁺", "≈", NNBSP, "*", "**", "e", "T", ":", "~", "&", "%", "\\%", "😀"]


def _generated(n: int = 1500) -> list[str]:
    rng = random.Random(20260930)
    return ["".join(rng.choice(_TOKENS) for _ in range(rng.randint(1, 16))) for _ in range(n)]


def _run(live, fn: str, inputs: list[str]) -> list[str]:
    payload = json.dumps(json.dumps(inputs))     # a JS string literal holding the JSON list
    return live.evaluate(
        f"(async () => {{ const m = await import('{MODULE}'); const xs = JSON.parse({payload});"
        f" return xs.map((x) => m.{fn}(x)); }})()")


def test_the_console_mirror_gives_every_shared_case_the_servers_answer(live):
    got = _run(live, "plain", [c["in"] for c in CASES])
    wrong = [(c["why"], c["out"], g) for c, g in zip(CASES, got) if g != c["out"]]
    assert not wrong, wrong


def test_the_console_mirror_agrees_with_the_server_on_generated_text(live):
    inputs = _generated()
    got = _run(live, "plain", inputs)
    wrong = [(s, plain(s), g) for s, g in zip(inputs, got) if g != plain(s)]
    assert not wrong, wrong[:5]


def test_a_number_stays_on_the_line_with_its_unit(live):
    [out] = _run(live, "prettyText", [
        "Air at 40 m/s and 15 °C through a 32 mm pipe, Re ≈ 2 × 10^6, with 5 prism layers."])
    assert out == (f"Air at 40{NBSP}m/s and 15{NBSP}°C through a 32{NBSP}mm pipe, "
                   f"Re ≈{NBSP}2{NBSP}×{NBSP}10⁶, with 5 prism layers.")


def test_the_joins_never_touch_code_or_a_thin_space_the_server_chose(live):
    out = _run(live, "prettyText", ["use `5 mm` here", "\\(15\\,^\\circ\\mathrm{C}\\)"])
    assert out == ["use `5 mm` here", f"15{NNBSP}°C"]


def test_the_assistant_bubble_shows_latex_as_an_engineer_prints_it(live):
    shown = live.evaluate("""(async () => {
      const { Stage } = await import('/static/js/render/stage.js');
      Stage.chat('assistant', 'Shall I target **\\\\(y^+=30\\\\text{–}300\\\\)** on `wing_1` at 40 m/s?');
      Stage.chat('user', 'y+ 30-300 is fine');
      const txt = [...document.querySelectorAll('.im.assistant .txt')].pop();
      const bub = [...document.querySelectorAll('.im.user .bub')].pop();
      return {strong: txt.querySelector('strong').textContent,
              code: txt.querySelector('code').textContent,
              text: txt.textContent, user: bub.textContent};
    })()""")
    assert shown["strong"] == f"y⁺ ={NBSP}30–300"     # a relation stays with its number
    assert shown["code"] == "wing_1", "a patch name stopped being code"
    assert f"40{NBSP}m/s" in shown["text"]
    assert "\\(" not in shown["text"] and "\\text" not in shown["text"]
    assert shown["user"] == "y+ 30-300 is fine", "the user's own words were rewritten"
