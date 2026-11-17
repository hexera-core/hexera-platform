# Responsibility: Verify CFDREQ-8 - the flow conditions the user confirmed (the intake's conditions step: fluid, speed, wall treatment, typed or clicked) reach the mesh: the plan's one sentence is read back at the run, its y+ sizes the first layer on every engine that sizes one, the user's own notes win, and an engine with no layers says so.
# Boundaries: pure over plain data and temporary workspaces; no model, no mesher is run.
from __future__ import annotations

import asyncio
import math

from tests.unit.engines.test_cfd_requirements import _read, _snappy, re_num

from meshpipeline.contracts import flow_conditions as FC
from meshpipeline.engines import cfd_requirements as CR
from meshpipeline.engines import mesh_requirements as MR

EXT = ("External flow around the car, confirmed by the user on the geometry stage. "
       + FC.sentence("air at 20 °C", "10 m/s", "functions", internal=False) + " The flow travels along +x.")
INT_RESOLVED = ("Internal flow through the elbow, confirmed by the user on the geometry stage. "
                + FC.sentence("water at 20 °C", "1 m/s", "resolved", internal=True) + " Openings: inlet (inlet).")


def test_the_sentence_is_read_back_as_written():
    got = FC.read(EXT)
    assert got["fluid"] == "air" and got["velocity_ms"] == 10.0 and got["wall"] == "functions"
    assert got["y_plus"] == 50.0 and got["nu_m2s"] == FC.NU["air"] and got["why"] == ""
    got = FC.read(INT_RESOLVED)
    assert got["fluid"] == "water" and got["velocity_ms"] == 1.0 and got["y_plus"] == 1.0
    # the plan's words say what the first layer does
    assert "sized for y⁺ ≈ 50, inside 30–300" in EXT and "sized for y⁺ ≈ 1" in INT_RESOLVED
    # a plan the model wrote in its own words has no such sentence: nothing is read
    assert FC.read("External aero over a car at 30 m/s with wall functions.") is None


def test_speeds_are_turned_into_metres_per_second_or_the_reason_is_said():
    assert math.isclose(FC.speed_ms("72 km/h", "air")[0], 20.0)
    assert math.isclose(FC.speed_ms("Mach 0.5", "air")[0], 0.5 * FC.SOUND_AIR)
    assert FC.speed_ms("Mach 0.5", "water")[0] is None
    inlet = [{"name": "in", "type": "inlet", "diameter_mm": 50}, {"name": "out", "type": "outlet"}]
    v, why = FC.speed_ms("6 l/s", "water", inlet)
    assert why == "" and math.isclose(v, 6e-3 / (math.pi * 0.05 ** 2 / 4))
    v, why = FC.speed_ms("6 l/s", "water", [])
    assert v is None and "inlet" in why
    # a fluid we have no viscosity for: the plan says the first layer keeps the mesher's sizing, and why
    said = FC.sentence("oil", "3 m/s", "functions", internal=True)
    assert "keeps the mesher's own sizing, because we have no viscosity for oil" in said
    assert FC.read(said)["why"] == "we have no viscosity for oil"


def test_no_conditions_sentence_leaves_the_reading_untouched():
    sec = MR.section("none")
    assert CR.with_conditions(sec, {"request_txt": "Structural FEA of the bracket."}) is sec
    assert CR.with_conditions(sec, {}) is sec


def test_the_conditions_alone_ask_for_the_first_layer(tmp_path):
    sec = CR.with_conditions(MR.section("none"), {"request_txt": EXT})
    req = MR.requirements_of(sec)
    assert req is not None and req["layers"]["y_plus"] == 50.0
    assert req["physics"]["velocity_ms"] == 10.0 and req["physics"]["fluid"] == "air"
    row = req["rows"][0]
    assert row["field"] == "layers:y_plus" and row["from"] == CR.CONDITIONS_FROM
    assert MR.words(sec)[0].startswith("From your flow conditions: y+ of about 50 at the walls")
    assert "(from your flow conditions)" in MR.line({**row, "met": None, "delivered": "x"})


def test_snappy_sizes_the_first_layer_from_the_conditions(tmp_path):
    sec = CR.with_conditions(MR.section("none"), {"request_txt": INT_RESOLVED})
    ask = CR.Ask(MR.requirements_of(sec), "snappy")
    ask.length_m, ask.length_from = 0.05, "the bore"
    text, _s = _snappy(tmp_path / "c", ask)
    h = re_num(text, "firstLayerThickness")
    expected, missing, _how = CR.y_plus_height(1.0, {"velocity_ms": 1.0, "fluid": "water"}, length_m=0.05)
    assert missing == [] and math.isclose(h, expected, rel_tol=1e-4)
    assert ask.rows["c1"]["status"] == "applied" and "first layer" in ask.rows["c1"]["built"]


def test_cfmesh_and_netgen_take_it_and_the_engines_without_layers_say_so():
    req = MR.requirements_of(CR.with_conditions(MR.section("none"), {"request_txt": EXT}))
    a = CR.Ask(req, "cfmesh")
    out = CR.cfmesh_strategy(a, {"n_layers": 3}, length_m=4.0)
    assert out["first_layer_thickness"] > 0 and a.rows["c1"]["status"] == "applied"
    a = CR.Ask(req, "netgen")
    out = CR.netgen_inputs(a, layers=3, port_diameters=[0.05], soft_budget=1_000_000, cell_limit=8_000_000)
    assert out["first_layer"] > 0 and a.rows["c1"]["status"] == "applied"
    for engine, fn in (("gmsh", lambda x: CR.gmsh_sizes(x, 0.01, external=True)),
                       ("snapgrid", lambda x: CR.snapgrid_plan(x, {"max_cells": 1_000_000})),
                       ("vmtk", lambda x: CR.vmtk_spec(x, {"boundary_layers": 5}))):
        a = CR.Ask(req, engine)
        fn(a)
        assert a.rows["c1"]["status"] == "not_applied", engine


def test_the_users_own_notes_win_over_the_conditions():
    req, _d, _na = _read("y+ around 5", layers=[{"what": "y_plus", "value": 5, "unit": "none", "quote": "y+ around 5"}])
    sec = CR.with_conditions(MR.section("used", requirements=req), {"request_txt": EXT})
    got = MR.requirements_of(sec)
    assert got["layers"]["y_plus"] == 5 and [r["field"] for r in got["rows"]] == ["layers:y_plus"]
    assert "the y+ you gave (5) is used" in MR.words(sec)[0]
    req, _d, _na = _read("first layer 0.1 mm", layers=[{"what": "first_height", "value": 0.1, "unit": "mm",
                                                        "quote": "first layer 0.1 mm"}])
    sec = CR.with_conditions(MR.section("used", requirements=req), {"request_txt": EXT})
    assert MR.requirements_of(sec)["layers"]["y_plus"] is None


def test_the_run_reads_the_conditions_with_no_notes(tmp_path):
    # before CFDREQ-8 a run with no notes wrote nothing here: the conditions step's answer reached no engine
    asyncio.run(CR.read_for_attempt(tmp_path, {"request_txt": INT_RESOLVED, "purpose": "internal_cfd"}))
    req = CR.requirements_for(tmp_path)
    assert req is not None and req["layers"]["y_plus"] == 1.0 and req["physics"]["velocity_ms"] == 1.0
