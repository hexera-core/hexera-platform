# Responsibility: Verify CFDREQ-9 - a first layer far thinner than the wall cells (a resolved wall, y+ about 1) is one snappyHexMesh keeps: its stack gets the layers that fill the thickness our own plan gives the wall, the layer stage's determinant bar sits under what so flat a cell reads, the rows say both, and a first layer the cells already keep (the rotor of job 9c638f7a) is left exactly as it was.
# Boundaries: pure over dictionary text and a small staged wall written here; no OpenFOAM run (the replay on job f4019933's own mesh is in the commit message).
from __future__ import annotations

import json
import math
import re
from pathlib import Path

import pytest

from meshpipeline.engines import cfd_requirements as CR
from meshpipeline.engines import mesh_requirements as MR


def _read(notes: str, **fields):
    base = {"stated_unit": "none", "sizes": [], "counts": [], "layers": [], "local": [], "physics": [],
            "element_kind": [], "limits": [], "other": []}
    base.update(fields)
    return MR.check(json.dumps(base), notes, route="flow")


def _num(text: str, key: str, inside: str = "") -> float | None:
    if inside:
        m = re.search(rf"{inside}\s*\{{[^}}]*\}}", text)
        text = m.group(0) if m else ""
    m = re.search(rf"\b{key}\s+([-+0-9.eE]+)\s*;", text)
    return float(m.group(1)) if m else None


#: the internal case's layer and quality blocks as render_internal_case writes them (balanced quality)
DICT = """addLayersControls { relativeSizes true; layers { wall { nSurfaceLayers 5; } }
  expansionRatio 1.2; finalLayerThickness 0.3; minThickness 0.05; nGrow 0; featureAngle 130; }
meshQualityControls { maxNonOrtho 65; maxBoundarySkewness 20; maxInternalSkewness 4; maxConcave 80;
  minVol 1.43e-14; minTetQuality 1e-13; minArea -1; minTwist 0.02; minDeterminant 0.001;
  minFaceWeight 0.02; minVolRatio 0.01; minTriangleTwist -1; nSmoothScale 4; errorReduction 0.75;
  relaxed { maxNonOrtho 68; maxInternalSkewness 4; minTetQuality -1e30; } }
"""

FIRST_MM = 0.0224          # job f4019933: y+ 1 for water at 1 m/s in a 1.66 m reducer
CELL_M = 0.00436           # the near-wall band's cells it grew into


def _first(mm: float = FIRST_MM, count: int | None = None):
    layers = [{"what": "first_height", "value": mm, "unit": "mm", "quote": f"first layer {mm:g} mm"}]
    notes = f"first layer {mm:g} mm"
    if count is not None:
        layers.append({"what": "count", "value": count, "unit": "none", "quote": f"{count} layers"})
        notes += f", {count} layers"
    req, _d, _na = _read(notes, layers=layers)
    return CR.Ask(req, "snappy")


def test_the_stack_from_a_thin_first_layer_fills_our_own_stack():
    n, first, stack = CR.layer_bridge(FIRST_MM * 1e-3, CELL_M, n_plan=5, growth=1.2, last_rel=0.3)
    # our relative stack: 5 layers up to a last layer 0.3 of the cell, 1.08 cells thick
    assert stack == pytest.approx(0.3 * CELL_M * sum(1.2 ** -k for k in range(5)))
    assert n == 21 and first == pytest.approx(FIRST_MM * 1e-3)
    built = first * (1.2 ** n - 1) / 0.2
    assert built == pytest.approx(stack, rel=0.12)
    # the last layer meets the cell above it as ours does (snappy's face-weight bar is 0.02)
    assert first * 1.2 ** (n - 1) / CELL_M > 0.1


def test_past_the_most_layers_the_first_layer_is_the_thinnest_that_fills_the_stack():
    n, first, stack = CR.layer_bridge(1e-6, 0.01, n_plan=5, growth=1.2, last_rel=0.3)
    assert n == CR.MAX_BRIDGE_LAYERS and first > 1e-6
    assert first * (1.2 ** n - 1) / 0.2 == pytest.approx(stack)
    # room for fewer cells than the layers need: the count stops at what the room holds
    n, first, _s = CR.layer_bridge(FIRST_MM * 1e-3, CELL_M, n_plan=5, growth=1.2, last_rel=0.3,
                                   faces=100_000, room=600_000)
    assert n == 11 and first > FIRST_MM * 1e-3
    # never fewer than the plan's own
    assert CR.layer_bridge(1e-3, 0.002, n_plan=5, growth=1.2, last_rel=0.3)[0] == 5


def test_a_thin_first_layer_gets_its_layers_and_a_determinant_bar_it_can_pass():
    ask = _first()
    text = CR.snappy_dict(ask, DICT, wall_cell_m=CELL_M, bridge={"faces": 110_000, "room": 3_000_000})
    assert _num(text, "nSurfaceLayers") == 21 and _num(text, "firstLayerThickness") == pytest.approx(2.24e-5)
    # the flat first layer reads about 27 (h / w)^2: 0.0007 on 4.4 mm faces, under our 0.001 bar
    det = _num(text, "minDeterminant", inside="relaxed")
    assert det is not None and det < 27 * (2.24e-5 / (2 * CELL_M)) ** 2
    assert _num(text, "minDeterminant") == 0.001              # the snap keeps our own bar
    row = ask.rows[ask.rid("layers:first_height")]
    assert row["status"] == "applied" and row["layers"] == 21
    assert "21 layers (not our usual 5)" in row["built"] and "not kept with only 5" in row["built"]


def test_a_y_plus_that_cannot_be_kept_as_asked_says_the_y_plus_it_gives():
    req, _d, _na = _read("y+ around 1, water at 1 m/s, pipe 1.66 m",
                         layers=[{"what": "y_plus", "value": 1, "unit": "none", "quote": "y+ around 1"}],
                         physics=[{"what": "water", "value": 0, "unit": "none", "quote": "water at 1 m/s"},
                                  {"what": "velocity", "value": 1, "unit": "m/s", "quote": "water at 1 m/s"},
                                  {"what": "length", "value": 1.66, "unit": "m", "quote": "pipe 1.66 m"}])
    ask = CR.Ask(req, "snappy")
    text = CR.snappy_dict(ask, DICT, wall_cell_m=0.05, bridge={"faces": 1_000, "room": 10_000_000})
    assert _num(text, "nSurfaceLayers") == CR.MAX_BRIDGE_LAYERS
    row = ask.rows[ask.rid("layers:y_plus")]
    assert row["status"] == "nearest" and re.search(r"y\+ about [0-9.]+, not 1", row["built"])
    assert row["first_m"] > row["asked_m"]


def test_the_users_own_count_is_kept_and_the_determinant_bar_still_follows_the_layer():
    ask = _first(count=5)
    text = CR.snappy_dict(ask, DICT, wall_cell_m=CELL_M, bridge={"faces": 110_000, "room": 3_000_000})
    assert _num(text, "nSurfaceLayers") == 5
    assert _num(text, "minDeterminant", inside="relaxed") < 0.001


def test_layers_the_cells_already_keep_are_left_exactly_as_they_were():
    """Job 9c638f7a's rotor: a 0.027 mm first layer under 1.25 mm wall cells (46 times thinner) kept 81.6%."""
    ask = _first(0.027)
    text = CR.snappy_dict(ask, DICT, wall_cell_m=0.00125, bridge={"faces": 50_000, "room": 3_000_000})
    assert _num(text, "nSurfaceLayers") == 5 and _num(text, "minDeterminant", inside="relaxed") is None
    # with no bridge (the external and multi-region cases) the count is the plan's, whatever the ratio
    ask = _first()
    text = CR.snappy_dict(ask, DICT, wall_cell_m=CELL_M)
    assert _num(text, "nSurfaceLayers") == 5


def test_a_determinant_limit_of_the_users_holds_the_layer_stage_too():
    req, _d, _na = _read("first layer 0.0224 mm, determinant over 0.01",
                         layers=[{"what": "first_height", "value": FIRST_MM, "unit": "mm",
                                  "quote": "first layer 0.0224 mm"}],
                         limits=[{"measure": "determinant", "shape": "any", "direction": "min", "target": 0.01,
                                  "allowed_percent": 0, "outer": 0, "has_outer": False,
                                  "quote": "determinant over 0.01"}])
    text = CR.snappy_dict(CR.Ask(req, "snappy"), DICT, wall_cell_m=CELL_M, bridge={"faces": 1e5, "room": 3e6})
    assert _num(text, "minDeterminant", inside="relaxed") is None


def _cylinder_stl(path: Path, r: float = 0.15, length: float = 1.0, n: int = 64) -> None:
    from meshpipeline.cad.stl_io import write_stl_binary
    tris = []
    for i in range(n):
        a0, a1 = 2 * math.pi * i / n, 2 * math.pi * (i + 1) / n
        p = [(0.0, r * math.cos(a), r * math.sin(a)) for a in (a0, a1)]
        q = [(length, y, z) for _x, y, z in p]
        tris += [(p[0], p[1], q[1]), (p[0], q[1], q[0])]
    path.parent.mkdir(parents=True, exist_ok=True)
    write_stl_binary(path, tris)


def test_the_internal_case_bridges_from_the_cells_its_layers_grow_into(tmp_path):
    from meshpipeline.engines.snappy.snappy_runner import render_internal_case
    ws = tmp_path / "case"
    (ws / "system").mkdir(parents=True)
    _cylinder_stl(ws / "constant" / "triSurface" / "wall.stl")
    names = {"wall": "wall", "inlet": "inlet", "outlet": "outlet"}
    ask = _first()
    summary = render_internal_case(
        ws, names=names, features={k: f"{k}.eMesh" for k in names}, interior_point=(0.5, 0.01, 0.01),
        bbox_min=(0.0, -0.15, -0.15), bbox_max=(1.0, 0.15, 0.15), base_cell=0.0175, surface_level=1,
        feature_level=2, n_layers=5, wall_key="wall", requirements=ask)
    text = (ws / "system" / "snappyHexMeshDict").read_text()
    base = max((summary["domain_max"][i] - summary["domain_min"][i]) / summary["divisions"][i] for i in range(3))
    cell = base / 2 ** summary["near_band_level"]
    assert summary["near_band_level"] > summary["surface_level"][1] and cell < 0.006
    # bridged from the band's cells (half the wall level's), with the wall's own area for the room
    n = int(_num(text, "nSurfaceLayers"))
    assert abs(n - CR.layer_bridge(FIRST_MM * 1e-3, cell, n_plan=5, growth=1.2, last_rel=0.3)[0]) <= 1 and n > 15
    assert _num(text, "minDeterminant", inside="relaxed") < 0.001
    row = ask.rows[ask.rid("layers:first_height")]
    assert f"{n} layers (not our usual 5)" in row["built"]
    # nothing asked: the case it always wrote
    ws2 = tmp_path / "bare"
    (ws2 / "system").mkdir(parents=True)
    render_internal_case(ws2, names=names, features={k: f"{k}.eMesh" for k in names}, interior_point=(0.5, 0.01, 0.01),
                         bbox_min=(0.0, -0.15, -0.15), bbox_max=(1.0, 0.15, 0.15), base_cell=0.0175,
                         surface_level=1, feature_level=2, n_layers=5, wall_key="wall")
    bare = (ws2 / "system" / "snappyHexMeshDict").read_text()
    assert "nSurfaceLayers 5;" in bare and "minDeterminant 0.001;" in bare and "relaxed { maxNonOrtho" in bare
