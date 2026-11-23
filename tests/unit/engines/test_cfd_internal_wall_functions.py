"""CFDREQ-18: an internal flow's wall cells are chosen with the wall-function first layer the y+ asks for."""

from __future__ import annotations

from meshpipeline.contracts import flow_conditions as FC
from meshpipeline.engines import cfd_requirements as CR
from meshpipeline.engines import mesh_requirements as MR


def _int_text(speed: str = "1 m/s", treatment: str = "functions") -> str:
    return ("Internal flow through the part, confirmed by the user on the geometry stage. "
            + FC.sentence("water at 20 °C", speed, treatment, internal=True, wall_said=False)
            + f" {FC.ACCEPTED_LEAD} the fluid, its speed, the wall treatment.")


def _ask(speed: str = "1 m/s", treatment: str = "functions", engine: str = "snappy") -> CR.Ask:
    sec = CR.with_conditions(MR.section("none"), {"request_txt": _int_text(speed, treatment), "intake_patches": [],
                                                  "purpose": "internal_cfd"})
    return CR.Ask(MR.requirements_of(sec), engine)


def test_the_bore_cells_make_room_for_the_wall_function_first_layer():
    # the CFD board's venturi (00122027_003): a 44.5 mm bore, water at 1 m/s, y+ 50 - the plan's 68 cells across
    # left 0.65 mm wall cells, and the wall was resolved at y+ 0.5 against the 50 asked
    ask, D = _ask(), 0.04447
    assert ask.treatment == "functions" and (ask.req.get("physics") or {}).get("flow") == "internal"
    n = CR.wall_function_bore_cells(ask, bore_D=D, cells_across=68)
    assert CR.WALL_FUNCTION_MIN_CELLS_ACROSS <= n < 68
    h, _missing, _how = CR.y_plus_height(50.0, ask.req.get("physics"), length_m=D, length_from="the bore")
    assert h is not None and h <= CR.FIRST_CAP_REL * D / n
    fit = CR.wall_fit(ask, h, D / n)
    assert fit["treatment"] == "functions" and 30.0 <= fit["y_plus"] <= 100.0
    assert "fits under them" in ask.wall_level_note


def test_the_bore_cells_never_drop_under_the_passage_floor_and_leave_other_asks_alone():
    D = 0.025                                   # a 25 mm bore: a y+ 50 layer would want ~7 cells across
    assert CR.wall_function_bore_cells(_ask(), bore_D=D, cells_across=40) == CR.WALL_FUNCTION_MIN_CELLS_ACROSS
    assert CR.wall_function_bore_cells(_ask(), bore_D=D, cells_across=10) == 10      # never more cells
    assert CR.wall_function_bore_cells(_ask(treatment="resolved"), bore_D=0.04447, cells_across=68) == 68
    mine = _ask()
    mine.wall_m = 0.001                          # a wall size of the user's own stands
    assert CR.wall_function_bore_cells(mine, bore_D=0.04447, cells_across=68) == 68
    assert CR.wall_function_bore_cells(None, bore_D=0.04447, cells_across=68) == 68


def test_cfmesh_fits_its_stack_under_half_the_wall_cell():
    ask, D = _ask(engine="cfmesh"), 0.04447
    n = CR.wall_function_bore_cells(ask, bore_D=D, cells_across=68, cap_rel=CR.CFMESH_STACK_REL)
    h, _m, _h = CR.y_plus_height(50.0, ask.req.get("physics"), length_m=D, length_from="the bore")
    assert h is not None and n == CR.WALL_FUNCTION_MIN_CELLS_ACROSS       # the floor binds: the thickest that fits
    fit = CR.wall_fit(ask, h, D / n, cap_rel=CR.CFMESH_STACK_REL)
    assert fit["treatment"] == "functions" and fit["y_plus"] >= 30.0
    assert CR.wall_fit(ask, h, D / 68, cap_rel=CR.CFMESH_STACK_REL)["treatment"] == "resolved"   # the old wall cell
