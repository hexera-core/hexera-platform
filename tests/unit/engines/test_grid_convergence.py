"""Richardson extrapolation and the GCI, checked against sequences whose answer is known.

THE POINT OF TESTING THIS AGAINST ANALYTIC CASES is that there is nothing to compare the output to
otherwise. A convergence band is a number nobody can check by looking at it, and a wrong one is
read as a guarantee. So the tests build sequences of the form phi(h) = exact + C*h**p for a known
exact and a known p, and insist the procedure recovers both.

devtools/ is not an importable package, so the module is loaded by path.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[3] / "devtools" / "semiconductor" / "convergence.py"


def _load():
    spec = importlib.util.spec_from_file_location("_convergence_under_test", _SRC)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def V():
    if not _SRC.is_file():
        pytest.skip(f"no {_SRC}")
    return _load()


def _sequence(V, exact, coefficient, order, h_fine=1.0e-5, r=2.0, n=3):
    """n grids of a sequence that converges to `exact` at exactly `order`, fine to coarse."""
    grids = []
    for i in range(n):
        h = h_fine * r ** i
        grids.append(V.Grid(label=f"h*{r ** i:g}", cells=int(1e6 / r ** (3 * i)), h=h,
                            value=exact + coefficient * h ** order))
    return grids


# #
# the arithmetic, against sequences whose answer is known
# #
def test_a_second_order_sequence_gives_order_two_and_the_exact_value(V):
    # phi = 350 + 1e9*h**2. With r = 2 the ratio of successive differences is exactly 4, so
    # p = ln(4)/ln(2) = 2, and the extrapolation is exact rather than approximate.
    r = V.analyse("T", _sequence(V, exact=350.0, coefficient=1.0e9, order=2.0))
    assert r.behaviour == "monotone"
    assert r.order == pytest.approx(2.0, abs=1e-9)
    assert r.extrapolated == pytest.approx(350.0, abs=1e-6)
    assert r.ok


def test_a_first_order_sequence_gives_order_one(V):
    r = V.analyse("T", _sequence(V, exact=20.0, coefficient=1.0e4, order=1.0))
    assert r.order == pytest.approx(1.0, abs=1e-9)
    assert r.extrapolated == pytest.approx(20.0, abs=1e-9)


def test_the_band_brackets_the_extrapolated_value(V):
    # The whole purpose: the band on the finest mesh has to contain the value the sequence is
    # heading for, or it is not an error estimate.
    r = V.analyse("T", _sequence(V, exact=350.0, coefficient=1.0e9, order=2.0))
    lo, hi = r.band
    assert lo <= r.extrapolated <= hi


def test_the_band_is_the_error_on_the_fine_grid_not_the_change_between_grids(V):
    # The raw change between two meshes is what gets quoted by someone who has not done this, and
    # it is not an error estimate. For phi = A + C*h**2 the fine mesh's true error is C*h**2 while
    # the change to the next mesh is 3*C*h**2, so the raw change overstates it threefold; the GCI
    # divides that out and applies the safety factor, landing at 1.25x the true error.
    exact, c, h = 350.0, 1.0e9, 1.0e-5
    r = V.analyse("T", _sequence(V, exact=exact, coefficient=c, order=2.0, h_fine=h))
    true_error = c * h ** 2 / r.fine.value
    assert r.relative_change == pytest.approx(3.0 * true_error, rel=1e-6)
    assert r.gci == pytest.approx(V.SAFETY_FACTOR * true_error, rel=1e-6)
    assert r.gci < r.relative_change, "at p=2, r=2 the raw change is the pessimistic one"


def test_a_slowly_converging_sequence_makes_the_raw_change_the_optimistic_number(V):
    # The direction flips with the observed order, which is the reason both are printed. At r = 2
    # the GCI is 1.25/(2**p - 1) times the change: bigger than it for p under about 1.3. A cut-cell
    # mesh of a chip-cooling part converges slowly, so this is the case that applies in practice
    # and the raw change is the one that would have been quoted.
    r = V.analyse("T", _sequence(V, exact=350.0, coefficient=1.0e2, order=0.5))
    assert r.order == pytest.approx(0.5, abs=1e-9)
    assert r.gci > r.relative_change
    assert r.gci == pytest.approx(
        V.SAFETY_FACTOR * r.relative_change / (2.0 ** 0.5 - 1.0), rel=1e-9)


def test_a_non_constant_refinement_ratio_still_recovers_the_order(V):
    # Real meshes do not land on exact ratios: the cell size is whatever the octree produced. With
    # r21 != r32 the order appears on both sides of its own equation and has to be solved for.
    exact, c, p = 350.0, 1.0e9, 2.0
    hs = (1.0e-5, 1.66e-5, 2.76e-5)
    grids = [V.Grid(label=f"h{i}", cells=1000, h=h, value=exact + c * h ** p)
             for i, h in enumerate(hs)]
    r = V.analyse("T", grids)
    assert r.order == pytest.approx(2.0, abs=1e-3)
    assert r.extrapolated == pytest.approx(exact, rel=1e-6)


# #
# everything it must refuse to answer
# #
def test_two_grids_is_not_a_study(V):
    r = V.analyse("T", _sequence(V, 350.0, 1.0e9, 2.0, n=2))
    assert not r.ok
    assert r.behaviour == "too few grids"
    assert r.band is None


def test_an_unconverged_run_poisons_the_study(V):
    # The difference between two unfinished solves is not a discretisation error. Measured: the
    # pin-fin part at 5 cells across had residuals still three decades above target at the cap.
    grids = _sequence(V, 350.0, 1.0e9, 2.0)
    grids[1] = V.Grid(label=grids[1].label, cells=grids[1].cells, h=grids[1].h,
                      value=grids[1].value, converged=False)
    r = V.analyse("T", grids)
    assert not r.ok
    assert "never met their residual targets" in " ".join(r.problems)


def test_grids_too_alike_to_subtract_are_refused(V):
    grids = _sequence(V, 350.0, 1.0e9, 2.0, r=1.05)
    r = V.analyse("T", grids)
    assert not r.ok
    assert r.behaviour == "grids too alike"


def test_an_oscillatory_sequence_gets_the_oscillation_and_no_order(V):
    # The answer went one way and then back. There is no monotone sequence to extrapolate along,
    # and the honest band is the oscillation's own amplitude.
    grids = [V.Grid(label="fine", cells=1000, h=1.0e-5, value=350.0),
             V.Grid(label="mid", cells=500, h=2.0e-5, value=352.0),
             V.Grid(label="coarse", cells=250, h=4.0e-5, value=351.0)]
    r = V.analyse("T", grids)
    assert r.behaviour == "oscillatory"
    assert r.order is None
    assert r.gci == pytest.approx(2.0 / 350.0)
    assert "oscillatory" in " ".join(r.notes)


def test_a_quantity_that_does_not_move_is_said_to_not_move(V):
    # The heat carried away is set by the boundary condition, so it should be mesh-independent.
    grids = [V.Grid(label=f"g{i}", cells=1000, h=1.0e-5 * 2 ** i, value=6.5) for i in range(3)]
    r = V.analyse("Q", grids)
    assert r.behaviour == "identical"
    assert r.gci == 0.0
    assert r.extrapolated == pytest.approx(6.5)


def test_a_non_finite_value_is_refused_at_construction(V):
    with pytest.raises(V.ConvergenceError):
        V.Grid(label="bad", cells=1000, h=1.0e-5, value=float("nan"))
    with pytest.raises(V.ConvergenceError):
        V.Grid(label="bad", cells=1000, h=0.0, value=1.0)


# #
# choosing which three grids, which is not "the three finest"
# #
def test_the_triple_is_chosen_for_spacing_and_not_only_fineness(V):
    # A part meshed at 2, 4, 5 and 8 cells across: the three finest are 8, 5, 4 whose ratios are
    # 1.66 and 1.21, and 1.21 is under the floor. 8, 4, 2 sit at 1.89 and 2.00 and work. Taking
    # the three finest would mean adding a run to a sweep made the study worse.
    hs = {2: 44.91e-6, 4: 23.8e-6, 5: 19.72e-6, 8: 11.9e-6}
    grids = [V.Grid(label=f"{n} across", cells=1000, h=h, value=350.0 + 1e9 * h ** 2)
             for n, h in hs.items()]
    chosen = V.best_triple(tuple(grids))
    assert [g.label for g in chosen] == ["8 across", "4 across", "2 across"]
    r21, r32 = V.ratios(chosen)
    assert min(r21, r32) >= V.MIN_REFINEMENT_RATIO
    # ...and the study on all four then succeeds, where the three finest would have been refused.
    assert V.analyse("T", grids).ok
    assert not V.analyse("T", [g for g in grids if g.label != "2 across"]).ok


def test_the_finest_mesh_is_always_in_the_triple(V):
    # The band goes on the finest mesh's answer, so a triple that leaves it out is answering about
    # a mesh nobody is going to use.
    hs = (10.0e-6, 20.0e-6, 40.0e-6, 80.0e-6, 160.0e-6)
    grids = [V.Grid(label=f"h{h * 1e6:.0f}", cells=1000, h=h, value=350.0 + 1e9 * h ** 2)
             for h in hs]
    chosen = V.best_triple(tuple(grids))
    assert chosen[0].h == min(hs)
