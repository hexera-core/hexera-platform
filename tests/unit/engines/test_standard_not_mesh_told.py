# Responsibility: Verify that a saved meshing standard's lines that are no mesh setting (NOTE-12 for the standard, STD-1) are told - before meshing, with the result and on the settings page - and that a solver named in such a line is told as delivered, never dropped without a word.
# Boundaries: pure over plain data: the standard's reading is fixed (no model), merged with no notes as a run merges it.
from __future__ import annotations

import json

from meshpipeline.engines import mesh_requirements as MR
from meshpipeline.engines import meshing_standard as MS

TEXT = "Global size 5 mm\nthis goes into a crash model\nNastran BDF output"


def _answer() -> str:
    return json.dumps({"stated_unit": "none", "sizes": [{"what": "global", "value": 5, "unit": "mm",
                                                     "quote": "Global size 5 mm"}],
                       "counts": [], "layers": [], "local": [], "physics": [], "element_kind": [], "limits": [],
                       "other": [{"quote": "this goes into a crash model", "kind": "not_mesh"},
                                 {"quote": "Nastran BDF output", "kind": "not_mesh"}],
                       "controls": [], "strictness": [], "applies_to": {"whole": ["any"], "lines": []}})


def test_a_line_that_is_no_mesh_setting_is_told_before_meshing_and_with_the_result():
    merged = MS.merge(MS.checked(_answer(), TEXT, route="fea"), MR.section("none"), route="fea")
    said = MR.live_words(merged)
    assert 'Not a mesh setting (your settings): "this goes into a crash model" - ' in " ".join(said)
    assert any(w.startswith(MR.NOT_MESH_LEAD) and "crash model" in w for w in MR.told_words(merged))


def test_a_solver_the_model_filed_as_no_mesh_setting_is_told_as_delivered():
    merged = MS.merge(MS.checked(_answer(), TEXT, route="fea"), MR.section("none"), route="fea")
    assert "From your settings: Nastran (.bdf) files - every FEA run delivers them." in merged["lines"]
    assert not any("Nastran BDF output" in w and w.startswith(MR.NOT_MESH_LEAD) for w in merged["lines"])


def test_the_settings_page_shows_it_too():
    shown = MS.understood(TEXT, {"key": MS.key_of(TEXT), "arguments": _answer()})
    assert shown[0] == "Global element size 5 mm."
    assert any(w.startswith('Not a mesh setting: "this goes into a crash model"') for w in shown)
    assert any(w.startswith("Nastran (.bdf) files") for w in shown)
