"""The five defects an independent review found in the conjugate work, pinned.

Every one of these was reachable from the documented command line and every one of them failed
SILENTLY: a wrong number with a clean verdict beside it, which is the only kind worth writing a
test for. Four were found by review rather than by use, so there is nothing else holding them.

cht.py lives in devtools/ and is not importable as a package, so it is loaded by path.
"""
import importlib.util
import math
import sys
from pathlib import Path

import pytest

from meshpipeline.engines.cfmesh import conjugate as C

_CHT = Path(__file__).resolve().parents[3] / "devtools" / "semiconductor" / "cht.py"


def _load_cht():
    spec = importlib.util.spec_from_file_location("_cht_under_test", _CHT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cht():
    if not _CHT.is_file():
        pytest.skip(f"no {_CHT}")
    return _load_cht()


# #
# a diverged run must not read as a converged one
# #
def test_a_nan_residual_is_unmet_and_not_invisible(cht):
    # OpenFOAM prints "Initial residual = -nan" when an equation blows up. A pattern that only
    # matches digits does not fail on that line, it SKIPS it: the field then has no residual, so
    # it is absent from the dict, so nothing checks it, and the worst possible run comes back
    # converged with a clean verdict.
    log = """
Time = 1

Solving for fluid region fluid
DILUPBiCGStab:  Solving for Ux, Initial residual = -nan, Final residual = -nan, No Iterations 1001
GAMG:  Solving for p_rgh, Initial residual = 1, Final residual = 0.01, No Iterations 8
Min/max T:300 350
"""
    iterations, residuals, _lo, _hi = cht.read_log(log)
    assert iterations == 1
    assert "fluid:Ux" in residuals, "the nan line was skipped, so nothing will ever check it"
    assert math.isnan(residuals["fluid:Ux"][1])
    unmet = dict((f, v) for f, v, _t in cht.SolveReport(residuals=residuals).unmet)
    assert "fluid:Ux" in unmet, "nan > target is False, so it has to be tested for explicitly"


def test_a_nan_heat_reading_is_a_gap_past_every_tolerance(cht):
    r = cht.SolveReport()
    assert r.heat_gap(6.5, float("nan")) == math.inf
    assert r.heat_gap(float("nan"), 6.5) == math.inf
    assert r.heat_gap(6.5, 6.4725) == pytest.approx(0.00423, rel=0.05)
    assert r.heat_gap(None, 6.5) is None


# #
# readings must belong to the run being judged
# #
def test_readings_left_by_an_earlier_solve_are_cleared(cht, tmp_path):
    # A chunked run writes one startTime directory per chunk, so the final row is whichever has the
    # greatest Time. Run the same workspace again and stop sooner, and the greatest Time in the
    # tree belongs to the PREVIOUS run: a slower re-solve reads back the faster run's mass flow and
    # temperature rise, prints them under the new velocity, and still reports that mass balances,
    # because both halves of every comparison are stale together.
    old = tmp_path / cht.POST / "fluid" / "inletFlow" / "750"
    old.mkdir(parents=True)
    (old / "surfaceFieldValue.dat").write_text(
        "# Time\tArea\tsum(phi)\n1000\t8.6e-08\t-8.90914543e-05\n")
    new = tmp_path / cht.POST / "fluid" / "inletFlow" / "0"
    new.mkdir(parents=True)
    (new / "surfaceFieldValue.dat").write_text(
        "# Time\tArea\tsum(phi)\n250\t8.6e-08\t-2.2272863e-05\n")

    # As it stands, the stale row wins, which is exactly why the tree has to be cleared.
    assert cht.dat_value(tmp_path, "inletFlow", "sum(phi)") == pytest.approx(-8.90914543e-05)
    assert cht.clear_post(tmp_path) == 2
    assert cht.dat_value(tmp_path, "inletFlow", "sum(phi)") is None
    assert cht.clear_post(tmp_path) == 0          # idempotent, and no error on a missing tree


def test_the_latest_chunk_wins_within_one_run(cht, tmp_path):
    # The other half of the same rule: inside ONE run the chunk that ran last is the answer.
    for start, time, value in ((0, 250, 1.0), (250, 500, 2.0), (500, 750, 3.0)):
        d = tmp_path / cht.POST / "fluid" / "outletFlow" / str(start)
        d.mkdir(parents=True)
        (d / "surfaceFieldValue.dat").write_text(
            f"# Time\tArea\tsum(phi)\n{time}\t8.6e-08\t{value}\n")
    assert cht.dat_value(tmp_path, "outletFlow", "sum(phi)") == pytest.approx(3.0)


def test_the_bulk_temperature_is_a_flux_weighted_mean(cht):
    # sum(phi*T) / sum(phi). Not weightedAreaAverage with phi as the weight, which multiplies by
    # the face area a second time because phi already has it.
    assert cht._bulk(-2.67e-02, -8.909e-05) == pytest.approx(299.7, rel=1e-3)
    assert cht._bulk(None, -8.909e-05) is None
    assert cht._bulk(-2.67e-02, 0.0) is None      # a sealed region divides by nothing


# #
# the volume gate has to see through the staircase, not be defeated by it
# #
def test_the_volume_tolerance_widens_with_coarseness_and_never_below_the_allowance():
    from meshpipeline.engines.staircase_error import single_passage_error_pct
    # A flat 5% asks a 2-cells-across mesh to be something it was never going to be: the measured
    # error there reaches 27.5%, so six of eight coarse meshes were refused for being coarse and
    # `check` never reached its solve.
    assert single_passage_error_pct(2.0) == pytest.approx(27.54)
    assert C.DEFAULT_VOLUME_TOL == 0.05
    # ...and at the resolution the platform's own rule asks for, nothing is loosened.
    assert single_passage_error_pct(13.0) * 1.5 / 100 < C.DEFAULT_VOLUME_TOL


def test_a_coarse_mesh_is_not_refused_for_being_coarse():
    # unit_cell_100um at 2 cells across, measured: 1.88392 mm3 against a CAD 2.600, which is
    # -27.54%. Past a flat 5% and inside the 41% the measurements support at that resolution.
    report = C.ConjugateReport(
        fluid=C.RegionReport("fluid", cells=20800, volume_m3=1.88392e-09, pieces=1, mesh_ok=True),
        solid=C.RegionReport("solid", cells=88400, volume_m3=6.56608e-09, pieces=1, mesh_ok=True),
        interface_faces_fluid=23400, interface_faces_solid=23400)
    C.verify(report, expect_fluid_volume_m3=2.6e-09, expect_fluid_pieces=1, volume_tol=0.41)
    assert report.ok is True, report.problems
    # ...and a split that put metal in the coolant region is still caught by the same gate.
    wrong = C.ConjugateReport(
        fluid=C.RegionReport("fluid", cells=20800, volume_m3=6.5e-09, pieces=1, mesh_ok=True),
        solid=C.RegionReport("solid", cells=88400, volume_m3=1.9e-09, pieces=1, mesh_ok=True),
        interface_faces_fluid=23400, interface_faces_solid=23400)
    C.verify(wrong, expect_fluid_volume_m3=2.6e-09, expect_fluid_pieces=1, volume_tol=0.41)
    assert any("not the coolant" in p for p in wrong.problems)
