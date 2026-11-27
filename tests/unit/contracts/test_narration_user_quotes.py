# Responsibility: Verify the live commentary's leak guard judges OUR words only - a told line that quotes the user's own words ("this goes into a crash model", a mesher's name they typed) is said, while our own sentence naming an internal thing is still stopped.
# Boundaries: pure; the publisher is a stand-in that records what it was given.
from __future__ import annotations

import asyncio
import json

from meshpipeline.contracts import narration as N
from meshpipeline.engines import mesh_requirements as MR
from meshpipeline.engines import meshing_standard as MS


def _said(line: str, quoted=()) -> list[str]:
    got: list[str] = []

    class Publish:
        async def anarrate(self, kind, words, **_kw):
            got.append(words)

    asyncio.run(N.asay(Publish(), line, quoted=quoted))
    return got


def test_a_users_quote_with_model_or_a_mesher_survives():
    for line in ('From your settings, kept as guidance for the mesh planning: "this goes into a crash model".',
                 'Not applied: "use gmsh for the inlet" - no mesh setting of ours matches it yet.'):
        assert _said(line, MR.live_quotes(line)) == [line]
        # before: the guard judged the user's words as ours and the line was dropped without a word
        assert _said(line) == []


def test_our_own_words_are_still_judged():
    # the words around the quote are ours: a mesher's name there is still stopped
    line = 'Not applied: "a fine mesh" - snappyHexMesh cannot do it.'
    assert _said(line, MR.live_quotes(line)) == []
    # a line of ours that quotes nothing of the user's is judged whole, quotes or not
    ours = 'From your settings: the "model" step is off.'
    assert MR.live_quotes(ours) == () and _said(ours, MR.live_quotes(ours)) == []
    assert N.leaks("The model chose 4 mm") and N.leaks('The model chose "4 mm"', quoted=["4 mm"])


def test_the_crash_model_line_is_said_before_meshing_on_every_route():
    text = "Global size 5 mm\nthis goes into a crash model"
    answer = json.dumps({"stated_unit": "none", "sizes": [{"what": "global", "value": 5, "unit": "mm",
                                                         "quote": "Global size 5 mm"}],
                         "counts": [], "layers": [], "local": [], "physics": [], "element_kind": [], "limits": [],
                         "other": [{"quote": "this goes into a crash model", "kind": "not_mesh"}],
                         "controls": [], "strictness": [], "applies_to": {"whole": ["any"], "lines": []}})
    for route in ("fea", "flow"):
        merged = MS.merge(MS.checked(answer, text, route=route), MR.section("none"), route=route)
        lines = MR.live_words(merged)
        said = [w for line in lines for w in _said(line, MR.live_quotes(line))]
        assert any("crash model" in w for w in said), (route, lines)
