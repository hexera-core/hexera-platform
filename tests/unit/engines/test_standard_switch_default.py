# Responsibility: Verify STD-4 as the owner ruled it - typed words beat the Shell elements switch nobody touched ("tria mesh only" in the box gives triangles while the switch sits on its default Quads), a switch the user SET beats the standard's words, the stage beats both; and that a row this route cannot take ("5 boundary layers" on an FEA run, files in mm) is never said before meshing as applied.
# Boundaries: pure over plain data and the decide() seam with a kept reading (no model is called).
from __future__ import annotations

import asyncio
import json

from meshpipeline.application import shell_elements as SE
from meshpipeline.engines import mesh_requirements as MR
from meshpipeline.engines import meshing_standard as MS

TRIA = "tria mesh only"


def _answer(**over) -> str:
    base = {"stated_unit": "none", "sizes": [], "counts": [], "layers": [], "local": [], "physics": [],
            "element_kind": [], "limits": [], "other": [], "controls": [], "strictness": [],
            "applies_to": {"whole": ["any"], "lines": []}}
    base.update(over)
    return json.dumps(base)


def _decide(text: str, answer: str, **snap) -> dict:
    std = MS.snapshot(text, {"key": MS.key_of(text), "arguments": answer}, **snap)
    return asyncio.run(MS.decide({MS.STATE_KEY: std}, route="fea", enabled=False))


def _tria_answer() -> str:
    return _answer(element_kind=[{"what": "triangles_only", "value": 0, "quote": TRIA}])


def test_typed_triangles_beat_a_switch_left_at_its_default():
    assert MR.kind_in_words(TRIA) == "triangles"  # the reader's fixed phrases read it (K02)
    sec = _decide(TRIA, _tria_answer())  # the page's switch left on Quads, its default
    assert MS.elements_of(sec)["kind"] == "triangles" and MS.elements_of(sec)["from"] == "settings"
    assert "From your settings: shell elements: triangles only." in sec["lines"]
    assert not any("the Shell elements switch says" in w for w in sec["lines"])
    # the stage starts where the words point, said as the settings'
    std = MS.snapshot(TRIA, None)
    assert SE.start(standard=std) == {"value": "triangles", "from": "settings"}


def test_a_switch_the_user_set_beats_the_words_and_the_stage_beats_both():
    sec = _decide(TRIA, _tria_answer(), shell_elements="quads", shell_elements_set=True)
    assert MS.elements_of(sec)["kind"] == "quads"
    assert any("the Shell elements switch says Quads" in w for w in sec["lines"])
    staged = MS.stage(_decide(TRIA, _tria_answer(), shell_elements="quads", shell_elements_set=True), "triangles")
    assert MS.elements_of(staged)["kind"] == "triangles" and MS.elements_of(staged)["from"] == "stage"


def test_a_switch_left_alone_with_no_words_is_still_no_standard():
    assert MS.snapshot("", None, shell_elements="quads") is None
    assert MS.snapshot("x", None, shell_elements="quads") == {"text": "x"}
    assert MS.switch_chosen({"text": "x"}) == "" and MS.switch_chosen({"shell_elements": "triangles"}) == "triangles"


def test_what_this_route_cannot_take_is_not_said_as_applied_before_meshing():
    text = "5 boundary layers\nexport in mm\nGlobal size 5 mm"
    answer = _answer(sizes=[{"what": "global", "value": 5, "unit": "mm", "quote": "Global size 5 mm"}],
                     layers=[{"what": "count", "value": 5, "quote": "5 boundary layers"}],
                     controls=[{"key": "file_units", "value": 0, "choice": "mm", "quote": "export in mm"}])
    fea = MS.merge(MS.checked(answer, text, route="fea"), MR.section("none"), route="fea")
    assert "From your settings: global element size 5 mm." in fea["lines"]
    assert not any(w.startswith("From your settings") and ("boundary layers" in w or " mm." in w and "solver" in w)
                   for w in fea["lines"])
    assert any(w.startswith(f"{MR.NOT_HERE_LEAD} (your settings): 5 boundary layers - a solid or shell mesh has no "
                            "boundary layers") for w in fea["lines"]), fea["lines"]
    assert any(r.get("field") == "control:file_units" for r in fea["requirements"]["rows"])
    assert any(w.startswith(f"{MR.NOT_HERE_LEAD} (your settings): solver files in mm - every file is written in "
                                "metres") for w in fea["lines"]), fea["lines"]
    # on the flow route boundary layers are the engines' own: said as set, as before
    flow = MS.merge(MS.checked(answer, text, route="flow"), MR.section("none"), route="flow")
    assert any(w.startswith("From your settings: 5 boundary layers") for w in flow["lines"]), flow["lines"]
