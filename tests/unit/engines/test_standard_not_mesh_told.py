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
    # STD-6: it is handed to the run's AI steps as guidance, and told so before meshing and with the result
    assert f'{MS.GUIDANCE_LEAD}: "this goes into a crash model".' in merged["lines"]
    assert any(w.startswith(MS.GUIDANCE_LEAD) and "crash model" in w for w in MR.told_words(merged))
    assert said is not None


def test_a_solver_the_model_filed_as_no_mesh_setting_is_told_as_delivered():
    merged = MS.merge(MS.checked(_answer(), TEXT, route="fea"), MR.section("none"), route="fea")
    assert "From your settings: Nastran (.bdf) files - every FEA run delivers them." in merged["lines"]
    assert not any("Nastran BDF output" in w and w.startswith(MR.NOT_MESH_LEAD) for w in merged["lines"])


def test_the_settings_page_shows_it_too():
    shown = MS.understood(TEXT, {"key": MS.key_of(TEXT), "arguments": _answer()})
    assert shown[0] == "Global element size 5 mm."
    assert 'Kept as guidance for the mesh planning: "this goes into a crash model".' in shown
    assert any(w.startswith("Nastran (.bdf) files") for w in shown)


def test_on_the_fea_routes_a_size_in_one_place_goes_to_a_runs_notes_and_a_count_is_the_default():
    text = "max 500k elements\n2 mm at the holes"
    answer = json.dumps({"stated_unit": "none", "sizes": [], "layers": [], "physics": [], "element_kind": [],
                         "limits": [], "other": [], "controls": [], "strictness": [],
                         "counts": [{"what": "ceiling", "value": 500000, "quote": "max 500k elements"}],
                         "local": [{"where": "the holes", "what": "size", "value": 2, "unit": "mm",
                                    "quote": "2 mm at the holes"}],
                         "applies_to": {"whole": ["any"], "lines": []}})
    fea = MS.merge(MS.checked(answer, text, route="fea"), MR.section("none"), route="fea")
    # a place belongs to one part: only the run's notes are read for it (cad/part_words), never said as set
    assert [w for w in fea["lines"] if MS.ELSEWHERE_WORDS in w] == [
        f'Not applied (your settings): "2 mm at the holes" - {MS.ELSEWHERE_WORDS}.']
    assert fea["requirements"]["local"] == []
    # STD-1: the count is the team's default count of every run (engines/fea_driver.element_target)
    assert "From your settings: at most 500,000 elements." in fea["lines"]
    assert fea["requirements"]["cells"]["ceiling"] == 500000
    # on the flow route both are the engines' own fields, applied as before
    flow = MS.merge(MS.checked(answer, text, route="flow"), MR.section("none"), route="flow")
    assert flow["requirements"]["cells"]["ceiling"] == 500000 and len(flow["requirements"]["local"]) == 1
