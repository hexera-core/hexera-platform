# Responsibility: Verify NOTE-13 - the controls declared once (contracts/mesh_controls) are read by the notes' one generic output with the usual checks, applied at each plan seam by the registry's appliers or told "not on this mesher" with its reason, and written as rows by each route's report: growth and elements through the thickness on FEA solids, cells across a bore and the wall's level on snappy, the files' unit (told, not offered yet).
# Boundaries: pure over plain data; no model, no mesher.
from __future__ import annotations

import json

from meshpipeline.contracts import mesh_controls as MC
from meshpipeline.engines import cfd_requirements as CR
from meshpipeline.engines import control_appliers as CA
from meshpipeline.engines import mesh_requirements as MR


def _read(notes: str, route: str = "fea", **fields):
    base = {"stated_unit": "none", "sizes": [], "counts": [], "layers": [], "local": [], "physics": [],
            "element_kind": [], "limits": [], "other": [], "controls": []}
    base.update(fields)
    return MR.check(json.dumps(base), notes, route=route)


def _ctl(key, quote, value=0, choice="", where=""):
    return {"key": key, "value": value, "choice": choice, "unit": "none", "where": where, "strict": False,
            "quote": quote}


def test_the_reader_is_generated_from_the_registry():
    keys = MR.tool()["function"]["parameters"]["properties"]["controls"]["items"]["properties"]["key"]["enum"]
    assert set(keys) == {c.key for c in MC.read_by_reader()} and "growth" not in keys     # growth has its own field
    assert 'key "wall_elements"' in MR.SYSTEM and "at least 3 elements through the thickness" in MR.SYSTEM


def test_a_control_is_read_with_the_usual_checks():
    req, dropped, _na = _read("At least 3 elements through the thickness. Then 4 elements through every wall.",
                              controls=[_ctl("wall_elements", "At least 3 elements through the thickness", 3),
                                        _ctl("wall_elements", "4 elements through every wall", 4)])
    assert dropped == [] and req["controls"]["wall_elements"]["value"] == 4               # the latest wins
    assert [r["asked"] for r in req["rows"]] == ["4 elements through the thickness of every wall"]
    assert not MR.is_empty(req)
    _req, dropped, _na = _read("3 elements through the thickness", controls=[_ctl("wall_elements", "3 elements through "
                                                                                  "the thickness", 5)])
    assert "not in those words" in dropped[0]
    _req, dropped, _na = _read("3 elements through the ribs", controls=[_ctl("wall_elements", "3 elements through the "
                                                                             "ribs", 3, where="the ribs")])
    assert "whole mesh only" in dropped[0]
    req, dropped, _na = _read("Export in mm.", controls=[_ctl("file_units", "Export in mm.", choice="mm")])
    assert dropped == [] and req["controls"]["file_units"]["choice"] == "mm"


def test_fea_solid_applies_growth_and_elements_through_the_wall():
    req, _d, _na = _read("growth rate 1.2, 3 elements through the thickness",
                         sizes=[{"what": "growth", "value": 1.2, "unit": "none", "quote": "growth rate 1.2"}],
                         controls=[_ctl("wall_elements", "3 elements through the thickness", 3)])
    zones = [{"id": "W1", "kind": "wall", "faces": ["Face_1"], "size_m": 0.0005, "thickness_m": 0.001,
              "why": "a 1 mm wall under Face_1: 2 elements through it"},
             {"id": "H1", "kind": "fillet", "faces": ["Face_2"], "size_m": 0.0004, "thickness_m": 0.001,
              "wall_elements": 3}]
    knobs = {"zones": zones, "growth": 1.3, "gentle": False}
    got = CA.apply("fea_solid", req, knobs)
    assert knobs["growth"] == 1.2 and got["growth"]["status"] == "applied"
    assert abs(zones[0]["size_m"] - 0.001 / 3) < 1e-12 and zones[0]["wall_elements"] == 3
    assert zones[1]["size_m"] == 0.0004                                                   # a hot spot keeps its own
    row = CA.report_row("fea_solid", "wall_elements", got)
    assert row["applied"] is True and row["met"] is None and "3 elements through each of 1 wall" in row["delivered"]
    # before NOTE-13 the solid's report said "not applied - on a solid, sizes grow by our own fixed rate"
    assert CA.report_row("fea_solid", "growth", got)["delivered"].startswith("applied as a setting")


def test_a_gentler_grading_the_ladder_needed_is_kept_and_said():
    knobs = {"zones": [], "growth": 1.2, "gentle": True}
    status, built = CA.fea_growth(knobs, 1.5)
    assert status == "nearest" and knobs["growth"] == 1.2 and "gentler grading" in built


def test_a_mesher_without_the_lever_says_so_with_the_registrys_reason(tmp_path):
    req, _d, _na = _read("3 elements through the thickness. Export in mm.",
                         controls=[_ctl("wall_elements", "3 elements through the thickness", 3),
                                   _ctl("file_units", "Export in mm.", choice="mm")])
    got = CA.apply("shell", req, {})
    assert got["wall_elements"]["status"] == "not_applied" and "one element through its thickness" in \
        got["wall_elements"]["built"]
    assert CA.report_row("fea_solid", "file_units")["delivered"].startswith("not applied - every file is written in "
                                                                             "metres")
    ask = CR.Ask(req, "cfmesh")
    ask.write(tmp_path)
    assert ask.rows[req["controls"]["wall_elements"]["id"]]["built"] == "a flow mesh has cells, not elements through a wall"


def test_snappy_takes_cells_across_the_bore_and_the_walls_level():
    req, _d, _na = _read("30 cells across the pipe, surface refinement level 3", route="flow",
                         controls=[_ctl("cells_across", "30 cells across the pipe", 30),
                                   _ctl("surface_level", "surface refinement level 3", 3)])
    ask = CR.Ask(req, "snappy")
    knobs = ask.controls({"internal": True, "cells_across": 24, "surface_level": 2, "feature_level": 3})
    assert knobs["cells_across"] == 30 and knobs["surface_level"] == 3 and knobs["feature_level"] == 4
    assert ask.rows[req["controls"]["cells_across"]["id"]]["status"] == "applied"
    req, _d, _na = _read("4 cells across the pipe", route="flow", controls=[_ctl("cells_across", "4 cells across the "
                                                                                "pipe", 4)])
    ask = CR.Ask(req, "snappy")
    assert ask.controls({"internal": True, "cells_across": 24, "surface_level": 2})["cells_across"] == 8
    assert ask.rows[req["controls"]["cells_across"]["id"]]["status"] == "nearest"
