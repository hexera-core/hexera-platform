"""v5's one path over the seq loop's fakes: a DIAGNOSED class sets the tournament's menu, the trace names it and
every candidate, the search_ai fallback runs only when the part is not clean. The real `diagnose` runs; only its
kernel call is faked."""

from __future__ import annotations

from typing import Any

import pytest
from tests.unit.cad.repair_v4.test_b24_seq_arm import _WS, _Judge, _local, _patched, _site

from meshpipeline.cad.repair_v4 import seq
from meshpipeline.cad.repair_v4 import sites as sites_mod
from meshpipeline.cad.repair_v5 import loop
from meshpipeline.cad.repair_v5.tournament import Guarded

#: `v5_pair_facts` as the kernel answers it (kernel face / solid indices): a shaft in a bore, 0.127 mm apart
SHAFT_IN_BORE = {"a": 1, "b": 2, "gap_mm": 0.127, "surface_a": "cylinder", "surface_b": "cylinder",
                 "radius_a_mm": 3.048, "radius_b_mm": 3.175, "axis_angle_deg": 0.0, "axis_offset_mm": 0.0,
                 "split_aligned_coaxial": True, "solid_a": 1, "solid_b": 2}


class _WSI(_WS):
    def __init__(self, q: int = 0, name: str = "w0") -> None:
        super().__init__(q, name)
        self.indices = {"F1": 1, "F2": 2, "B1": 1, "B2": 2}
        self.ids = dict.fromkeys(self.indices, "fp")


@pytest.fixture(autouse=True)
def _pair_facts(monkeypatch: pytest.MonkeyPatch) -> list:
    calls: list = []

    def call(pool: Any, fn: str, payload: dict) -> dict:
        calls.append((fn, payload))
        return {"pairs": [SHAFT_IN_BORE]}
    monkeypatch.setattr(sites_mod, "_call", call)
    return calls


def _recording(by: int, tried: list) -> Any:
    def apply(pool: Any, ws: Any, repair: Any, defect: Any, *, timeout_s: float, pair: Any = None,
              run_stage: Any = None, overrides: Any = ()) -> Any:
        tried.append((repair, tuple(overrides)))
        # the REAL derived parameters, with the variant's override, so the trace line is the one v5 writes
        params = [{**seq.params_for(op, defect)[1], **(dict(overrides[i]) if i < len(overrides) else {})}
                  for i, op in enumerate(repair)]
        return seq._Applied(_WSI(int(ws.labels["q"]) + by), [], ("B1",), params)
    return apply


def _run(kind: str = "overlapping_facets", *, verify: Any, fallback: Any = None) -> Any:
    judge, start = _Judge(), _WSI(0)
    site = _site("S@F1", kind="self_overlap", entities=["F1"],
                 failures=[{"mesher": "gmsh_default", "error_kind": kind}], graph={"mesher_faces": ["F1"]})
    return loop.repair_v5(None, start, "o.step", judge=judge, sites_of=lambda pool, ws, v: [site], h_mm=2.0,
                          start_verdict=judge.full(None, start, "o.step"), wall_s=1000.0, fallback=fallback,
                          run_stage=lambda *a, **k: None, verify=verify, guard=lambda *a: Guarded(None, 0.0))


def test_the_diagnosed_CLASS_sets_the_menu_the_seed_wins_the_tie_and_the_trace_names_it() -> None:
    tried: list = []
    with _patched(_recording(4, tried)):
        out = _run(verify=lambda *a, **k: _local())
    # a near-coaxial pair: the shared-angle split of BOTH faces is the seed (segments from the measured cause: 16),
    # then the class menu (step 7), then the variants - six entered, the rest logged as over the cap
    assert [r for r, _ in tried][:5] == list(loop.CLASS_REPAIRS["close_faces:coaxial"])
    assert len(tried) == 6 and [m.result for m in out.matches.records].count("not_entered") >= 1
    assert [m.result for m in out.matches.records if m.tier == "rule_seed"] == ["won"]
    assert out.matches.records[0].params == [{"segments": 16, "scope": "targets"}]
    assert out.clean and out.fallback is None and "class repairs made the part clean" in out.fallback_why
    assert out.seq.counters.row()["classes_fixed"] == {"close_faces": 1}
    assert any("class close_faces: overlapping_facets at near-coaxial: faces F1-F2" in line for line in out.lines())
    assert any("match [rule_seed] split_aligned{scope=targets, segments=16} -> won" in line for line in out.lines())


def test_the_tournament_reads_the_CLASS_row_not_the_code_row_and_logs_every_loser() -> None:
    """overlapping_facets' v4 row goes on to split_angle+condition; close_faces' menu is the class's own."""
    tried: list = []
    with _patched(_recording(0, tried)):
        out = _run(verify=lambda *a, **k: _local(gone=False))
    assert {r for r, _ in tried} == set(loop.CLASS_REPAIRS["close_faces:coaxial"])
    assert {m.result for m in out.matches.records} == {"still_there", "not_entered"}
    assert out.seq.counters.row()["classes_unrepaired"] == {"close_faces": 1}


def test_a_file_wall_kill_mid_tournament_stops_on_the_wall_and_keeps_the_best_state() -> None:
    """Step 7 (carried from step 6): the harness's SIGALRM kill raised while the tournament's pool runs used to lose
    the whole file; now the loop stops on the wall, the winner already had is kept, and the outcome says so."""
    class _Kill(BaseException):
        FILE_WALL = True

    base = _recording(4, [])

    def apply(pool: Any, ws: Any, repair: Any, defect: Any, **kw: Any) -> Any:
        got = base(pool, ws, repair, defect, **kw)
        got.ws.brep_path = f"/fake/{'+'.join(repair)}{len(kw.get('overrides') or ())}.brep"
        return got

    def verify(pool: Any, before: Any, after: Any, defect: Any, touched: Any = None, **kw: Any) -> Any:
        if not after.brep_path.endswith("/split_aligned0.brep"):
            raise _Kill("file wall clock 600s exceeded")
        return _local()

    with _patched(apply):
        out = _run(verify=verify)
    assert out.stopped_on_wall and "file wall clock" in out.seq.counters.wall_stop
    assert out.best.labels["q"] == 4            # the seed's repaired state, judged and kept
    assert [m.result for m in out.matches.records if m.tier == "rule_seed"] == ["won"]


def test_a_crash_with_nothing_located_is_checker_failed_not_a_defect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sites_mod, "_call", lambda pool, fn, payload: {"pairs": []})
    tried: list = []
    with _patched(_recording(4, tried)):
        out = _run("gmsh_crash", verify=lambda *a, **k: _local())
    assert tried == [] and out.seq.counters.row()["checker_failed"] == 1
    assert any("gmsh_crash at S@F1 -> checker_failed" in line for line in out.lines())
    assert "classes_found" not in out.seq.counters.row()


def test_the_fallback_runs_from_search_ais_start_when_not_clean_and_its_gain_is_kept() -> None:
    seen: dict = {}

    class _Result:
        changed, clean, reason = True, True, "clean"

        def __init__(self, ws: Any) -> None:
            self.best, self.best_verdict = ws, type("V", (), {"clean": True})()

    def fallback(best: Any, verdict: Any, left: float) -> Any:
        seen["start_q"], seen["left"] = best.labels["q"], left
        return _Result(_WSI(4, name="fb"))

    with _patched(_recording(1, [])):
        out = _run(verify=lambda *a, **k: _local(), fallback=fallback)
    # CRS v5 r2 fix I: the class state is no more VALID than the start, so search_ai's own start goes first
    assert seen["start_q"] == 0 and seen["left"] > loop.MIN_FALLBACK_S
    # CRS v5 r2: the fallback gets the search wall whole (1000 s here): the class loop's time is extra, never its
    assert seen["left"] == 1000.0
    assert out.clean and out.best.brep_path.endswith("fb.brep")


def test_unknown_keeps_v4s_code_rows_under_their_own_keys() -> None:
    table = loop.v5_table(legacy=seq.RULE_TABLE)
    assert table["unknown:open_shell"] == seq.RULE_TABLE["open_shell"]   # uncapped: the tournament caps and logs
    assert table["low_quality"] == loop.CLASS_REPAIRS["low_quality"]          # the class, not the code's row
