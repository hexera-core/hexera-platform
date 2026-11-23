# Responsibility: The deterministic sweep's sew rungs and sliver widths are made against the part's diagonal, so none
#                 can fall outside the window the kernel accepts, at any scale; and a part that admits no ladder gets
#                 no sew, visibly.
# Boundaries: hermetic; the window is spelled out here from the contract (1e-6 x diag .. min(0.1 mm, 0.001 x diag)),
#             not imported from the code under test.
from __future__ import annotations

import pytest

from meshpipeline.cad.repair_v4.ops.shells import scaled_sew_ladder, scaled_sliver_widths
from meshpipeline.cad.repair_v4.search import DetGenerator, SearchState
from meshpipeline.cad.repair_v4.stages import _Steps, _shells
from meshpipeline.cad.repair_v4.types import Site
from meshpipeline.cad.repair_v4.workspace import Workspace

#: watch component .. a 100 m assembly, decades apart (mm)
DIAGONALS = [0.5, 1.7, 17.3, 173.0, 1_732.0, 17_320.0, 99_000.0]


def _window(diag: float) -> tuple[float, float]:
    return 1e-6 * diag, min(0.1, 0.001 * diag)


@pytest.mark.parametrize("diag", DIAGONALS)
def test_every_rung_and_width_is_inside_the_kernel_window(diag: float) -> None:
    lo, hi = _window(diag)
    ladder = scaled_sew_ladder(diag)
    assert ladder, f"a {diag} mm part has a non-empty window and must get a ladder"
    assert ladder == sorted(ladder) and all(lo <= r <= hi for r in ladder), (ladder, lo, hi)
    widths = scaled_sliver_widths(diag)
    assert widths and all(0 < w <= hi for w in widths), (widths, hi)


def test_the_ladder_scales_with_the_part_not_with_the_constant() -> None:
    """Two parts three decades apart: the tight end of the ladder moves with the part's bound where it is relative
    (small part), and stays at the 0.1 mm cap's fractions where the cap binds (large part)."""
    small, big = scaled_sew_ladder(1.0), scaled_sew_ladder(1000.0)
    assert small[-1] == pytest.approx(1e-3)  # 0.001 x 1 mm: the relative bound binds
    assert big[-1] == pytest.approx(0.1)  # the absolute 0.1 mm cap binds
    assert small[0] == pytest.approx(1e-5)
    assert big[0] == pytest.approx(1e-3)


def test_a_part_over_the_window_gets_no_ladder_and_junk_diagonals_get_nothing() -> None:
    assert scaled_sew_ladder(200_000.0) == []  # 200 m: floor 0.2 mm is over the 0.1 mm cap
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        assert scaled_sew_ladder(bad) == [] and scaled_sliver_widths(bad) == ()


def _site(kind: str) -> Site:
    return Site(id="S1", kind=kind, entities=[], xyz=[], meshers_failing=[], dist_over_h=None, graph={}, view_png=None)


def _state(kind: str, brep: str = "/fake/a.brep") -> SearchState:
    ws = Workspace(brep_path=brep, ids={"F1": "fp1"}, labels={}, history=[], original="/fake/o.brep")
    return SearchState(id="S0", ws=ws, verdict=None, score=(0, 0, 0.0, 0.0, 0), sites=[_site(kind)], depth=0,
                       parent=None, via=None)


@pytest.mark.parametrize("diag", DIAGONALS)
def test_the_sweep_proposes_a_sew_inside_the_window_at_every_scale(diag: float) -> None:
    got = DetGenerator(diagonal=lambda ws, d=diag: d).propose(_state("open"), 50)
    sew = [c for c in got if c.op == "sew"]
    assert len(sew) == 1
    lo, hi = _window(diag)
    assert all(lo <= r <= hi for r in sew[0].params["tol_ladder"])
    from meshpipeline.cad.repair_v4.ops import validate_call

    assert validate_call("sew", sew[0].targets, sew[0].params, _state("open").ws) == []


def test_the_old_absolute_ladder_is_what_fails_at_173_mm() -> None:
    """The measurement behind the change: [1e-4, 1e-3, 1e-2] is a window miss on a 173 mm part."""
    lo, hi = _window(173.0)
    assert 1e-4 < lo
    assert not all(lo <= r <= hi for r in [1e-4, 1e-3, 1e-2])


def test_a_part_with_no_window_gets_no_sew_and_says_so() -> None:
    gen = DetGenerator(diagonal=lambda ws: 200_000.0)
    got = gen.propose(_state("open"), 50)
    assert "sew" not in {c.op for c in got} and {"make_volume", "orient_and_close"} <= {c.op for c in got}
    assert any("sew not proposed" in n and "kernel window" in n for n in gen.notes), gen.notes


def test_an_unmeasured_part_gets_no_sized_op_and_says_so() -> None:
    gen = DetGenerator()  # no diagonal bound: nothing is invented
    ops = {c.op for c in gen.propose(_state("open"), 50)} | {c.op for c in gen.propose(_state("sliver"), 50)}
    assert "sew" not in ops and "remove_small" not in ops
    assert any("diagonal is unknown" in n for n in gen.notes), gen.notes


def test_a_diagonal_the_kernel_cannot_give_is_noted_not_invented() -> None:
    from meshpipeline.cad.repair_v4.verdict.valid import CheckerUnavailable

    def broken(ws: Workspace) -> float:
        raise CheckerUnavailable("ws_diagonal: crashed")

    gen = DetGenerator(diagonal=broken)
    assert "sew" not in {c.op for c in gen.propose(_state("open"), 50)}
    assert any("ws_diagonal: crashed" in n for n in gen.notes)


def test_the_sweep_measures_each_workspace_once() -> None:
    calls: list[str] = []

    def measure(ws: Workspace) -> float:
        calls.append(ws.brep_path)
        return 100.0

    gen = DetGenerator(diagonal=measure)
    gen.propose(_state("open"), 50)
    gen.propose(_state("open"), 50)
    gen.propose(_state("open", "/fake/b.brep"), 50)
    assert calls == ["/fake/a.brep", "/fake/b.brep"]


class _Pool:
    def __init__(self, diag: float) -> None:
        self.diag, self.calls = diag, []

    def call(self, fn: str, payload: dict, timeout_s: float = 0.0):  # noqa: ANN201
        self.calls.append((fn, payload))

        class R:
            ok, status, error = True, "ok", None
            value = {"diag_mm": self.diag}

        return R()


def test_the_staged_shells_default_is_sized_against_the_part(monkeypatch: pytest.MonkeyPatch) -> None:
    """`DEFAULTS["shells"]` carried absolute mm; its sew ladder and sliver width now come from the part's diagonal."""
    from meshpipeline.cad.repair_v4 import stages

    seen: list[tuple[str, dict]] = []
    for diag in (1.7, 1_732.0):
        steps = _Steps(_Pool(diag), Workspace(brep_path="/fake/x.brep", ids={"F1": "fp"}, labels={}, history=[],
                                              original="/fake/o.brep"), 1.0)
        monkeypatch.setattr(steps, "apply", lambda op, targets, params, **kw: seen.append((op, params)) or False)
        _shells(steps, {**stages.DEFAULTS["shells"], "small": ["F1"]})
    removes = [p for op, p in seen if op == "remove_small"]
    sews = [p for op, p in seen if op == "sew"]
    assert removes[0]["max_width_mm"] == pytest.approx(0.5 * 0.0017, rel=1e-6)
    assert removes[1]["max_width_mm"] == pytest.approx(0.05)
    for p, diag in zip(sews, (1.7, 1_732.0), strict=True):
        lo, hi = _window(diag)
        assert all(lo <= r <= hi for r in p["tol_ladder"])
