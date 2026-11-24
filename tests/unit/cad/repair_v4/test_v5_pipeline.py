# Responsibility: CRS v5's one path (`v5.repair_v5`, `classes`) - the class decides the repair, at most two are
#                 tried, the trace names the class, and the search_ai fallback runs only when the part is not clean.
"""Tiny targeted tests over B24's fakes: the loop's own rules are B24's tests; these pin what v5 adds."""

from __future__ import annotations

from typing import Any

from tests.unit.cad.repair_v4.test_b24_seq_arm import _WS, _Judge, _local, _patched, _site

from meshpipeline.cad.repair_v4 import classes as C
from meshpipeline.cad.repair_v4 import v5


def _ws_site(code: str = "overlapping_facets") -> Any:
    return _site("S1", kind="invalid", entities=["F1"],
                 failures=[{"mesher": "gmsh_default", "error_kind": code}],
                 graph={"nodes": [{"id": "F1", "area": 4.4, "min_width": 0.4}], "mesher_faces": ["F1"]})


def _recording(by: int, tried: list) -> Any:
    from meshpipeline.cad.repair_v4 import seq

    def apply(pool: Any, ws: Any, repair: Any, defect: Any, *, timeout_s: float, pair: Any = None,
              run_stage: Any = None) -> Any:
        tried.append(repair)
        return seq._Applied(_WS(int(ws.labels["q"]) + by), [], ("B1",), [{} for _ in repair])
    return apply


def _run(code: str, *, by: int, verify: Any, fallback: Any = None, wall_s: float = 1000.0) -> Any:
    judge, start = _Judge(), _WS(0)
    return v5.repair_v5(None, start, "o.step", judge=judge, sites_of=lambda pool, ws, v: [_ws_site(code)],
                        start_verdict=judge.full(None, start, "o.step"), wall_s=wall_s, fallback=fallback,
                        run_stage=lambda *a, **k: None, verify=verify)


def test_the_CLASS_picks_the_repair_and_the_trace_names_it() -> None:
    tried: list = []
    with _patched(_recording(4, tried)):
        out = _run("overlapping_facets", by=4, verify=lambda *a, **k: _local())
    assert tried == [("split_aligned",)]
    assert out.clean and out.fallback is None and "class repairs made the part clean" in out.fallback_why
    assert out.seq.counters.row()["classes_fixed"] == {C.COARSE_CROSSING: 1}
    assert any(f"class {C.COARSE_CROSSING}" in line for line in out.lines())


def test_at_most_TWO_repairs_per_defect_then_it_is_unrepaired() -> None:
    tried: list = []
    with _patched(_recording(0, tried)):
        out = _run("overlapping_facets", by=0, verify=lambda *a, **k: _local(gone=False))
    assert len(tried) == C.MAX_REPAIRS
    assert out.seq.counters.row()["classes_unrepaired"] == {C.COARSE_CROSSING: 1}


def test_the_fallback_runs_from_the_loops_best_state_when_not_clean_and_its_gain_is_kept() -> None:
    seen: dict = {}

    class _Result:
        changed, clean, reason = True, True, "clean"

        def __init__(self, ws: Any) -> None:
            self.best, self.best_verdict = ws, type("V", (), {"clean": True})()

    def fallback(best: Any, verdict: Any, left: float) -> Any:
        seen["start_q"], seen["left"] = best.labels["q"], left
        return _Result(_WS(4, name="fb"))

    with _patched(_recording(1, [])):
        out = _run("overlapping_facets", by=1, verify=lambda *a, **k: _local(), fallback=fallback)
    assert seen["start_q"] >= 1 and seen["left"] > v5.MIN_FALLBACK_S
    assert out.clean and out.best.brep_path.endswith("fb.brep")


def test_a_code_no_class_claims_keeps_v4s_rows_capped_at_two() -> None:
    from meshpipeline.cad.repair_v4 import seq

    table = C.v5_table(legacy=seq.RULE_TABLE)
    assert table["open_shell"] == seq.RULE_TABLE["open_shell"][:2]
    assert table[C.COARSE_CROSSING][0] == ("split_aligned",)
    assert all(len(v) <= C.MAX_REPAIRS for v in table.values())
