# Responsibility: Verify a wall-function first layer grows into the wall's own cells when the near-wall band's cells
# are too thin to hold it without squashing the cell behind it (engines/snappy/drivers, rulebook 75).
# Boundaries: the driver's decision from the run's conditions; the mesh it gives is judged on the CFD board.
from __future__ import annotations

from types import SimpleNamespace

from meshpipeline.engines.snappy import drivers as D

#: the CFD board's tube bank: water at 1 m/s through a 116.8 mm inlet, wall functions (y+ 50 at the cell centre,
#: a 2.16 mm first layer by CFDREQ-13's estimate)
WATER = {"flow": "internal", "fluid": "water", "velocity_ms": 1.0, "hydraulic_diameter_m": 0.1168}


def _ask(**kw):
    base = {"treatment": "functions", "first_m": None, "y_plus": 50.0, "req": {"physics": WATER}}
    return SimpleNamespace(**{**base, **kw})


def test_a_thick_wall_function_layer_takes_the_band_away():
    why = D._wall_function_band_off(_ask(), wall_cell=0.00487, bore_D=0.1168)
    assert why.startswith("no near-wall band: a wall-function first layer about 2.2 mm high")
    assert "2.4 mm band cells" in why and "4.9 mm wall cells" in why


def test_the_band_stays_where_the_layer_fits_it_or_the_wall_is_resolved():
    # coarse wall cells: 2.16 mm is under 0.4 of the 10 mm band cell
    assert D._wall_function_band_off(_ask(), wall_cell=0.02, bore_D=0.1168) == ""
    # a resolved wall, no conditions, or no requirements at all: the band as planned
    assert D._wall_function_band_off(_ask(treatment="resolved"), wall_cell=0.00487, bore_D=0.1168) == ""
    assert D._wall_function_band_off(_ask(y_plus=None), wall_cell=0.00487, bore_D=0.1168) == ""
    assert D._wall_function_band_off(None, wall_cell=0.00487, bore_D=0.1168) == ""
    # a laminar flow has no wall-function target
    slow = {**WATER, "velocity_ms": 0.001}
    assert D._wall_function_band_off(_ask(req={"physics": slow}), wall_cell=0.00487, bore_D=0.1168) == ""


def test_the_user_s_own_first_layer_is_judged_the_same_way():
    assert D._wall_function_band_off(_ask(first_m=0.002, y_plus=None), wall_cell=0.00487, bore_D=0.1) != ""
    assert D._wall_function_band_off(_ask(first_m=0.0005, y_plus=None), wall_cell=0.00487, bore_D=0.1) == ""
