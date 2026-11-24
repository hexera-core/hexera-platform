# Responsibility: The deterministic sweep's sew rungs and sliver widths are made against the part's diagonal, so none
#                 can fall outside the window the kernel accepts, at any scale; and a part that admits no ladder gets
#                 no sew, visibly.
# Boundaries: hermetic; the window is spelled out here from the contract (1e-6 x diag .. min(0.1 mm, 0.001 x diag)),
#             not imported from the code under test.
from __future__ import annotations

import pytest

from meshpipeline.cad.repair_v4.ops.shells import scaled_sew_ladder, scaled_sliver_widths
from meshpipeline.cad.repair_v4.search import DetGenerator, SearchState
from meshpipeline.cad.repair_v4.stages import _shells, _Steps
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


# --- fill_loop on an open site (Task 42) ---------------------------------------------------------------------------


def _open_site_with(loops: list[tuple[list[str], float]], diagonal: float = 127.0) -> Site:
    return Site(id="S@open", kind="open", entities=[], xyz=[], meshers_failing=["valid"], dist_over_h=None,
                graph={"free_loops": [{"edges": e, "span_mm": s} for e, s in loops], "diagonal_mm": diagonal},
                view_png=None)


def _state_of(site: Site) -> SearchState:
    ids = {"F1": "fp1", **{f"E{i}": f"fpE{i}" for i in range(1, 9)}}
    ws = Workspace(brep_path="/fake/a.brep", ids=ids, labels={}, history=[], original="/fake/o.brep")
    return SearchState(id="S0", ws=ws, verdict=None, score=(0, 0, 0.0, 0.0, 0), sites=[site], depth=0, parent=None,
                       via=None)


def test_the_sweep_proposes_fill_loop_for_every_loop_whatever_its_size() -> None:
    """B12: the 5%-of-diagonal fill cap is gone; a loop spanning 60% of the part is proposed like a small one."""
    small, big = (["E1", "E2", "E3", "E4"], 4.24), (["E5", "E6"], 0.60 * 127.0)  # 3.3% and 60% of the diagonal
    gen = DetGenerator(diagonal=lambda ws: 127.0)
    got = [c for c in gen.propose(_state_of(_open_site_with([small, big])), 50) if c.op == "fill_loop"]
    assert [c.targets for c in got] == [small[0], big[0]] and got[0].params == {"continuity": "G0"}
    assert not any("fill cap" in n for n in gen.notes)
    from meshpipeline.cad.repair_v4.ops import REGISTRY, validate_call

    assert "fill_loop" in REGISTRY
    assert validate_call("fill_loop", got[0].targets, got[0].params, _state_of(_open_site_with([small])).ws) == []


def test_no_fill_size_refuses_a_loop_in_the_sweep() -> None:
    """B12: what used to sit just over the cap (span / 0.05 x 0.99 of the diagonal) is proposed like the rest."""
    span = 4.0
    loop = (["E1", "E2", "E3"], span)
    for diag in (span / 0.05, span / 0.05 * 0.99, span * 1.01):
        gen = DetGenerator(diagonal=lambda ws, _d=diag: _d)
        got = [c for c in gen.propose(_state_of(_open_site_with([loop], diag)), 50) if c.op == "fill_loop"]
        assert len(got) == 1, diag


def test_an_open_site_without_loops_says_so_and_still_proposes_the_others() -> None:
    gen = DetGenerator(diagonal=lambda ws: 127.0)
    got = gen.propose(_state_of(_open_site_with([])), 50)
    assert not [c for c in got if c.op == "fill_loop"] and {c.op for c in got} >= {"sew", "make_volume"}
    assert any("fill_loop not proposed" in n for n in gen.notes)


def test_a_no_solids_site_proposes_make_volume_and_other_invalid_sites_do_not() -> None:
    def invalid(codes: list[str]) -> Site:
        return Site(id="S@invalid", kind="invalid", entities=[], xyz=[], meshers_failing=["valid"], dist_over_h=None,
                    graph={"hard_codes": codes}, view_png=None)

    gen = DetGenerator(diagonal=lambda ws: 127.0)
    assert "make_volume" in {c.op for c in gen.propose(_state_of(invalid(["no_solids"])), 50)}
    assert "make_volume" not in {c.op for c in gen.propose(_state_of(invalid(["brep_invalid"])), 50)}


def _unbounded_notes(kind: str) -> list[str]:
    gen = DetGenerator(diagonal=lambda ws: 3.46e100)
    assert not {c.op for c in gen.propose(_state(kind), 50)} & {"sew", "remove_small"}
    return gen.notes


def test_an_unbounded_part_is_noted_as_unbounded_not_as_an_empty_window() -> None:
    for kind in ("open", "sliver"):
        notes = _unbounded_notes(kind)
        assert any("unbounded" in n and "unbounded_geometry" in n for n in notes), notes
        assert not any("kernel window" in n or "diagonal is unknown" in n for n in notes), notes


def test_a_large_but_bounded_part_keeps_its_window_note() -> None:
    gen = DetGenerator(diagonal=lambda ws: 5e8)  # 500 km: far over 100 m, still under the unbounded limit
    gen.propose(_state("open"), 50)
    assert any("kernel window" in n for n in gen.notes) and not any("unbounded" in n for n in gen.notes)


def test_the_staged_shells_note_an_unbounded_part_as_unbounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from meshpipeline.cad.repair_v4 import stages

    steps = _Steps(_Pool(3.46e100), Workspace(brep_path="/fake/x.brep", ids={"F1": "fp"}, labels={}, history=[],
                                              original="/fake/o.brep"), 1.0)
    applied: list[str] = []
    monkeypatch.setattr(steps, "apply", lambda op, targets, params, **kw: applied.append(op) or False)
    _shells(steps, {**stages.DEFAULTS["shells"], "small": ["F1"]})
    assert "sew" not in applied and "remove_small" not in applied
    assert sum("unbounded input" in n and "unbounded_geometry" in n for n in steps.notes) == 2, steps.notes


def test_an_unbounded_part_gets_no_fill_loop_and_says_why() -> None:
    gen = DetGenerator(diagonal=lambda ws: 3.46e100)
    got = gen.propose(_state_of(_open_site_with([(["E1", "E2", "E3"], 4.0)], diagonal=3.46e100)), 50)
    assert "fill_loop" not in {c.op for c in got}
    assert any("fill_loop not proposed" in n and "unbounded" in n for n in gen.notes), gen.notes


# --- A11 step 2 priority 3: the det sweep's remove_small widths lose the absolute ceiling ---------------------------


@pytest.mark.parametrize("diag", DIAGONALS)
def test_the_det_widths_are_diagonal_relative_with_no_absolute_ceiling(diag: float) -> None:
    """LEAD-2's condition (a): a 1 m part and a 10 mm part are treated alike, so every width is a fixed fraction of
    the diagonal and none is pinned to a constant."""
    from meshpipeline.cad.repair_v4.ops.shells import det_sliver_widths

    widths = det_sliver_widths(diag)
    assert list(widths) == sorted(widths) and all(w > 0 for w in widths)
    assert [w / diag for w in widths] == pytest.approx([1e-4, 1e-3, 1e-2])
    assert len(widths) == 3


def test_the_det_widths_reach_the_tiny_edges_the_old_ceiling_could_not() -> None:
    """Measured (A11): 6 of the 16 faces a low_quality failure located carry an edge of 0.001 to 0.2 mm, which is 1
    to 4 times the old min(0.1 mm, 1e-3 x diagonal) ceiling. The worst case is a 0.202 mm edge on a 46.9 mm part."""
    from meshpipeline.cad.repair_v4.ops.shells import det_sliver_widths, scaled_sliver_widths

    for diag, edge_mm in ((46.9, 0.202), (437.1, 0.129), (441.0, 0.110)):
        assert max(scaled_sliver_widths(diag)) < edge_mm, "the old ladder could not reach it"
        assert max(det_sliver_widths(diag)) >= edge_mm, (diag, edge_mm, det_sliver_widths(diag))


def test_the_staged_healer_keeps_the_OLD_ladder_so_the_baseline_does_not_move() -> None:
    """The staged arm is the baseline every snapshot compares a search arm against. Widening its removal would move
    that baseline and confound the comparison, so only the det sweep's ladder changed."""
    import inspect

    from meshpipeline.cad.repair_v4 import stages

    assert "scaled_sliver_widths" in inspect.getsource(stages._shells)
    assert "det_sliver_widths" not in inspect.getsource(stages._shells)


def test_widening_the_ladder_adds_no_candidates_per_expansion() -> None:
    """LEAD-2's condition (b): k and the op budget are what bind, so the width change must not cost candidates. It
    is the same three rungs, so a sliver site proposes exactly as many remove_small rows as before."""
    from meshpipeline.cad.repair_v4.ops.shells import det_sliver_widths, scaled_sliver_widths

    for diag in DIAGONALS:
        assert len(det_sliver_widths(diag)) == len(scaled_sliver_widths(diag)) == 3
