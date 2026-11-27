# Responsibility: Verify STD-5 on a standard written in sections ("FEA: ... / CFD: ..."): each section's lines apply to its own kind of mesh even when both sections give the same field (the flow section's base cell never replaces the FEA section's global size), and the line about what was left out names only the kinds of the lines left out.
# Boundaries: pure over plain data: the standard's reading is fixed (no model), merged as a run merges it.
from __future__ import annotations

import json

from meshpipeline.engines import mesh_requirements as MR
from meshpipeline.engines import meshing_standard as MS

FEA_LINE = "FEA: global size 4 mm, aspect ratio < 5"
CFD_LINE = "CFD: base cell 10 mm, 5 prism layers, maxNonOrtho 65"
H02 = f"{FEA_LINE}\n{CFD_LINE}"


def _h02() -> str:
    return json.dumps({
        "stated_unit": "none", "counts": [], "local": [], "physics": [], "element_kind": [], "other": [],
        "controls": [], "strictness": [],
        "sizes": [{"what": "global", "value": 4, "unit": "mm", "quote": "global size 4 mm"},
                  {"what": "global", "value": 10, "unit": "mm", "quote": "base cell 10 mm"}],
        "layers": [{"what": "count", "value": 5, "quote": "5 prism layers"}],
        "limits": [{"measure": "aspect_ratio", "shape": "any", "direction": "max", "target": 5, "allowed_percent": 0,
                    "outer": 0, "has_outer": False, "quote": "aspect ratio < 5"},
                   {"measure": "non_orthogonality", "shape": "any", "direction": "max", "target": 65,
                    "allowed_percent": 0, "outer": 0, "has_outer": False, "quote": "maxNonOrtho 65"}],
        "applies_to": {"whole": ["solid", "cfd"], "lines": [{"quote": FEA_LINE, "kinds": ["shell", "solid"]},
                                                           {"quote": CFD_LINE, "kinds": ["cfd"]}]}})


def _merged(route: str) -> dict:
    return MS.merge(MS.checked(_h02(), H02, route=route), MR.section("none"), route=route)


def test_each_section_sizes_its_own_kind_of_mesh():
    fea = _merged("fea")
    assert fea["requirements"]["sizes"]["global_mm"] == 4.0
    assert "From your settings: global element size 4 mm." in fea["lines"]
    flow = _merged("flow")
    assert flow["requirements"]["sizes"]["global_mm"] == 10.0


def test_the_left_out_line_names_only_the_kinds_of_the_lines_left_out():
    fea = _merged("fea")
    said = fea["standard_not_applied"]
    assert "written for flow (CFD) meshes" in said and "solid and" not in said, said
    assert '"base cell 10 mm"' in said and '"5 prism layers"' in said
    flow = _merged("flow")
    assert "written for shell and solid meshes" in flow["standard_not_applied"], flow["standard_not_applied"]
    assert "flow (CFD)" not in flow["standard_not_applied"]


def test_a_line_tagged_for_every_kind_list_is_unchanged():
    one = json.loads(_h02())
    one["applies_to"] = {"whole": ["any"], "lines": []}
    sec = MS.checked(json.dumps(one), H02, route="fea")
    assert "other_kinds" not in sec
