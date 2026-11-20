# Responsibility: Verify STD-5 (a saved standard applies to the kinds of mesh it is written for: a structural list on a flow mesh, or a CFD list on a solid, changes nothing and is told once; a mixed list splits line by line; the job's notes apply as typed), CFDREQ-11 (a limit for even cells is told, not judged, on a mesh with prism layers) and NOTE-14's finer-than-asked size (the row says asked, built and why).
# Boundaries: pure over plain data (the reader's answers as the model would give them) and a temporary workspace; no model, no mesher.
from __future__ import annotations

import json

from tests.unit.engines.test_mesh_requirements import CRITERIA, criteria_answer

from meshpipeline.engines import cfd_requirements as CR
from meshpipeline.engines import mesh_requirements as MR
from meshpipeline.engines import meshing_standard as MS

FOR_FLOW = "Your saved meshing standard is written for shell and solid meshes; it was not applied to this flow mesh."


def _std(answer: dict, text: str, route: str) -> dict:
    return MS.checked(json.dumps(answer), text, route=route)


def _structural(route: str = MR.FLOW) -> dict:
    return _std({**criteria_answer(), "applies_to": {"whole": ["shell", "solid"], "lines": []}}, CRITERIA, route)


def _notes(answer: dict, text: str, route: str = MR.FLOW) -> dict:
    req, dropped, na = MR.check(json.dumps(answer), text, route=route)
    return MR.section("used", requirements=req, dropped=dropped, not_applied=na)


def test_a_structural_list_on_a_flow_mesh_changes_nothing_and_is_told(tmp_path):
    got = MS.merge(_structural(), MR.section("none"), route=MR.FLOW)
    assert got["status"] == "none" and got["standard_not_applied"] == FOR_FLOW
    # the run is the run with no standard: nothing is kept for any engine, so snappyHexMesh, cfMesh and VMTK write the
    # dictionaries they write with no notes at all
    MR.write(tmp_path, got)
    assert CR.requirements_for(tmp_path) is None and not (tmp_path / MR.FILE).exists()
    MS.told_not_applied(tmp_path, got["standard_not_applied"])
    from meshpipeline.contracts.user_notes import read_remarks
    assert read_remarks(tmp_path) == ["From your settings: your saved meshing standard is written for shell and solid "
                                      "meshes; it was not applied to this flow mesh."]
    # on a solid or shell mesh the same list is applied as before
    fea = MS.merge(_structural("fea"), MR.section("none"), route="fea")
    assert fea["status"] == "used" and len(fea["requirements"]["rows"]) == 10 and "standard_not_applied" not in fea


def test_a_cfd_list_on_a_solid_mesh_is_not_applied_and_told():
    text = "Max 5 million cells\nnon-orthogonality under 65"
    answer = {"stated_unit": "none", "counts": [{"what": "ceiling", "value": 5000000, "quote": "Max 5 million cells"}],
              "limits": [{"measure": "non_orthogonality", "shape": "any", "direction": "max", "target": 65,
                          "allowed_percent": 0, "outer": 0, "has_outer": False, "quote": "non-orthogonality under 65"}],
              "applies_to": {"whole": ["cfd"], "lines": []}}
    got = MS.merge(_std(answer, text, "fea"), MR.section("none"), route="fea")
    assert got["status"] == "none"
    assert got["standard_not_applied"] == ("Your saved meshing standard is written for flow (CFD) meshes; it was not "
                                           "applied to this solid or shell mesh.")


def test_a_mixed_list_splits_by_line_and_the_notes_apply_as_typed():
    text = CRITERIA + "\nAt most 2 million cells or elements"
    answer = criteria_answer()
    answer["counts"] = [{"what": "ceiling", "value": 2000000, "quote": "At most 2 million cells or elements"}]
    answer["applies_to"] = {"whole": ["shell", "solid"],
                            "lines": [{"kinds": ["any"], "quote": "At most 2 million cells or elements"}]}
    notes = _notes({"layers": [{"what": "count", "value": 8, "unit": "none", "quote": "8 prism layers"}]},
                   "8 prism layers")
    got = MS.merge(_std(answer, text, MR.FLOW), notes, route=MR.FLOW)
    fields = sorted(r["field"] for r in got["requirements"]["rows"])
    assert fields == ["cells:ceiling", "layers:count"]                    # the general line and the notes, nothing else
    assert got["requirements"]["sizes"]["global_mm"] is None and got["requirements"]["limits"] == []
    assert got["lines"][0].startswith("10 lines of your saved meshing standard are written for shell and solid meshes "
                                      "and were not applied to this flow mesh: ")
    said = MS.remark(got, "Of the 2 things you asked for: 2 met.")
    assert said.startswith("From your settings and notes: Of the 2 things") and said.endswith(got["lines"][0])


def test_a_standard_read_before_the_tag_existed_applies_as_it_always_did():
    old = MS.merge(_std(criteria_answer(), CRITERIA, MR.FLOW), MR.section("none"), route=MR.FLOW)
    assert old["status"] == "used" and len(old["requirements"]["rows"]) == 10 and "standard_not_applied" not in old
    # a tag whose line is not the user's is no tag
    answer = {**criteria_answer(), "applies_to": {"whole": ["any"], "lines": [{"kinds": ["cfd"], "quote": "made up"}]}}
    assert "applies_to" not in MR.check(json.dumps(answer), CRITERIA)[0]


def test_an_aspect_ratio_limit_is_told_not_judged_on_a_mesh_with_prism_layers():
    lim = {"measure": "aspect_ratio", "shape": "any", "op": "<=", "target": 4.0, "share": 0.01, "outer": 6.0}
    missed = {"applied": True, "met": False, "delivered": "43.9% of cells (1,243,327) are over 4"}
    got = CR._layer_cells_said(lim, dict(missed), {"layer_coverage_pct": 100.0})
    assert got["met"] is None and got["layer_cells"] is True and "prism layer cells are thin by design" in got["delivered"]
    assert MR.summary({"rows": [{**got, "asked": "aspect ratio under 4"}]}) == (
        "Of the 1 thing you asked for: 0 met; 1 limit for even cells told, not judged, on a mesh with prism layers.")
    # no layers, a hard limit, or a CFD measure: judged as before
    assert CR._layer_cells_said(lim, dict(missed), {"layer_coverage_pct": 0.0}) == missed
    assert CR._layer_cells_said(lim, dict(missed), {"layer_coverage_pct": 99.0}, {"strictness": "hard"}) == missed
    ortho = {**lim, "measure": "non_orthogonality"}
    assert CR._layer_cells_said(ortho, dict(missed), {"layer_coverage_pct": 99.0}) == missed


def test_a_size_far_larger_than_the_part_says_asked_built_and_why():
    row = {"field": "global", "asked": "global element size 10 mm"}
    got = MR.finer_global(row, 10.0, 0.95, 13.0)
    assert got == {"met": None, "finer_than_asked": True,
                   "delivered": "elements are about 0.95 mm, finer than the 10 mm asked: your part is 13 mm across, so "
                                "its walls, holes and curves need smaller elements to keep their shape"}
    assert MR.summary({"rows": [{**row, "applied": True, **got}]}) == (
        "Of the 1 thing you asked for: 0 met; global element size 10 mm: " + got["delivered"] + ".")
    assert MR.finer_global({**row, "strictness": "hard"}, 10.0, 0.95, 13.0)["met"] is False
    assert MR.finer_global(row, 10.0, 6.0, 300.0) is None                # near the size asked: the row as it was


def test_a_shells_global_row_built_far_finer_never_says_coarser_than_our_rules():
    """Val4 under a saved 10 mm size: "elements are about 2.74 mm; this is coarser than our accuracy rules ask for on
    some faces" - built finer than asked, it said coarser. Now asked, built and why, as information."""
    import numpy as np

    from meshpipeline.engines import shell_requirements as SR
    from meshpipeline.engines.shell_mesh import ShellMesh
    n, h = 4, 3.0
    xyz = np.array([(i * h, j * h, 0.0) for j in range(n + 1) for i in range(n + 1)], float)
    quads = np.array([(j * (n + 1) + i, j * (n + 1) + i + 1, (j + 1) * (n + 1) + i + 1, (j + 1) * (n + 1) + i)
                      for j in range(n) for i in range(n)], dtype=np.int64)
    mesh = ShellMesh(xyz=xyz, quads=quads, trias=np.zeros((0, 3), dtype=np.int64), wall=np.full(len(quads), 2.0),
                     mid=["Mid_1"] * len(quads), part=["Part_1"] * len(quads))
    row = {"id": "s1", "field": "global", "asked": "global element size 10 mm", "quote": "Global Mesh Size = 10 mm"}
    got = SR._size_row(row, "global", mesh, {"coarser": True}, {"sizes": {"global_mm": 10.0}})
    assert "coarser than our accuracy rules" not in got["delivered"] and got["met"] is None
    assert got["delivered"] == ("elements are about 3 mm, finer than the 10 mm asked: your part is 12 mm across, so its "
                                "walls, holes and curves need smaller elements to keep their shape")
