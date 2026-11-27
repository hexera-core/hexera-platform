"""v5 step 6, the conformal assembly pass: the fuzzy schedule and its cap, the tweak bound, the Boolean by volume, the
keep/revert rule, `tweak`'s aim, and the pass end to end over fake ops (no kernel)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from meshpipeline.cad.repair_v4 import seq
from meshpipeline.cad.repair_v4.local_verify import Defect
from meshpipeline.cad.repair_v4.ops import REGISTRY
from meshpipeline.cad.repair_v4.workspace import Diff, Workspace
from meshpipeline.cad.repair_v5 import conformal as C
from meshpipeline.cad.repair_v5.tournament import Guarded


def test_fuzzy_steps_start_at_the_kernel_precision_climb_the_measured_gaps_and_stay_under_the_cap() -> None:
    assert C.fuzzy_schedule([0.01, 0.002, 0.002], cap_mm=1.0) == pytest.approx([1e-7, 0.0022, 0.011])
    # a step at or over the smallest real gap would close it: dropped, and so is everything after it
    assert C.fuzzy_schedule([0.01, 0.002], cap_mm=0.005) == pytest.approx([1e-7, 0.0022])
    assert C.fuzzy_schedule([0.5], cap_mm=1e-7) == []
    assert len(C.fuzzy_schedule([0.001 * k for k in range(1, 20)], cap_mm=1.0)) == C.MAX_FUZZY_STEPS


def test_the_cap_is_the_smallest_gap_over_the_near_bound_else_the_reach() -> None:
    pairs = [{"gap_mm": 0.0}, {"gap_mm": 0.05}, {"gap_mm": 0.4}, {"gap_mm": 0.3}]
    assert C.cap_of(pairs, bound_mm=0.1, reach_mm=1.0) == 0.3
    assert C.cap_of(pairs[:2], bound_mm=0.1, reach_mm=1.0) == 1.0
    assert C.near_bound(50.0) == pytest.approx(0.05) and C.near_bound(5000.0) == 0.1


def test_tweak_moves_the_smaller_body_only_for_parallel_faces_inside_the_bound_once_per_body() -> None:
    pairs = [{"a": 1, "b": 2, "gap_mm": 0.02, "parallel": True},
             {"a": 1, "b": 3, "gap_mm": 0.01, "parallel": False},      # not coincident faces: not a misalignment
             {"a": 2, "b": 4, "gap_mm": 0.03, "parallel": True},       # 2 already in a tweak
             {"a": 5, "b": 6, "gap_mm": 0.2, "parallel": True},        # over the bound: a real gap
             {"a": 7, "b": 8, "gap_mm": 0.0, "parallel": True}]        # already touching
    volumes = {"1": 10.0, "2": 5.0}
    assert C.tweak_plan(pairs, volumes, bound_mm=0.1) == [(2, 1, 0.02)]


def test_the_boolean_is_chosen_by_volume() -> None:
    assert C.boolean_for(60.0, 100.0, 1000.0, "B1", "B2")[0] == "fuse"
    op, targets, params, why = C.boolean_for(10.0, 100.0, 1000.0, "B1", "B2")
    assert (op, targets, params["mode"]) == ("conformalise", ["B1", "B2"], "cut_by:B2") and "smaller body B1" in why


def _facts(hard: tuple = (), panel: tuple = (), clean: bool = False) -> C.PassFacts:
    v = type("V", (), {"valid": type("R", (), {"hard": [type("F", (), {"code": c})() for c in hard]})(),
                       "panel": [type("M", (), {"name": n, "status": s, "error_kind": k})() for n, s, k in panel],
                       "clean": clean})()
    return C.facts_of(v)


BEFORE = _facts(("body_overlap", "body_overlap"), (("gmsh_prod", "fail", "edge_through_face"),
                                                    ("netgen_occ", "pass", None)))


def test_kept_when_overlaps_drop_with_nothing_new() -> None:
    after = _facts(("body_overlap",), (("gmsh_prod", "fail", "edge_through_face"), ("netgen_occ", "pass", None)))
    assert C.keep_decision(BEFORE, after)[:2] == (True, "won")
    assert C.keep_decision(BEFORE, _facts((), (), clean=True))[:2] == (True, "won")


@pytest.mark.parametrize(("after", "refused", "result"), [
    (_facts(("body_overlap", "self_intersection")), None, "new_defect"),              # a new hard code
    (_facts((), (("netgen_occ", "fail", "other"),)), None, "new_defect"),             # a passing mesher now fails
    (BEFORE, None, "still_there"),                                                     # nothing dropped
    (_facts(), "catastrophic_loss: part kept 0.02", "guard_refused"),                  # the loss guard
])
def test_reverted_on_a_new_code_a_new_failure_no_drop_or_the_guard(after: C.PassFacts, refused: str | None,
                                                                    result: str) -> None:
    keep, got, _why = C.keep_decision(BEFORE, after, refused)
    assert (keep, got) == (False, result)


def test_tweak_aims_at_two_bodies_and_says_why_not_at_faces() -> None:
    bodies = Defect(id="s", kind="validity", code="body_overlap", entities=("B1", "B2"),
                    measurement={"diagonal_mm": 50.0})
    assert seq.aim_reason("tweak", bodies) is None
    assert seq.params_for("tweak", bodies) == (["B1", "B2"], {"max_mm": pytest.approx(0.05)})
    faces = Defect(id="s", kind="mesher", code="edge_through_face", entities=("F1",))
    assert "tweak aims at body" in (seq.aim_reason("tweak", faces) or "")
    with pytest.raises(seq.NoParameters):
        seq.params_for("tweak", Defect(id="s", kind="validity", code="x", entities=("B1",)))


# --- the pass over fake ops -----------------------------------------------------------------------------------


def _ws(ids: tuple = ("B1", "B2", "B3"), history: list | None = None, name: str = "w0") -> Workspace:
    return Workspace(brep_path=f"/tmp/{name}.brep", ids=dict.fromkeys(ids, "fp"), labels={}, history=history or [],
                     original="o.step", indices={s: i + 1 for i, s in enumerate(ids)}, part_diag_mm=100.0)


@dataclass
class _Op:
    name: str
    decline: set = field(default_factory=set)        # fuzzy values (or "any") it declines at
    calls: list = field(default_factory=list)
    params_schema: dict = field(default_factory=lambda: {"type": "object"})

    def apply(self, pool: Any, ws: Workspace, targets: list, params: dict, *, timeout_s: float) -> Any:
        self.calls.append((list(targets), dict(params)))
        if "any" in self.decline or params.get("fuzzy_mm") in self.decline:
            return ws, Diff(op=self.name, params={"declined": "no_shared_interface"}, modified={}, generated={},
                            removed=[], declared_added=[], declared_removed=[], seconds=0.0)
        new_id = f"B{9 + len(ws.history)}"
        diff = Diff(op=self.name, params={**params, "measured": {"moved_mm": 0.02}}, modified={targets[0]: [new_id]},
                    generated={}, removed=[], declared_added=[], declared_removed=[], seconds=0.0)
        ids = tuple(s for s in ws.ids if s != targets[0]) + (new_id,)
        return _ws(ids, [*ws.history, diff], name=f"w{len(ws.history) + 1}"), diff


class _Pool:
    def call(self, fn: str, payload: dict, *, timeout_s: float) -> Any:
        assert fn == "v5_body_gaps"
        # B1-B2 misaligned 0.02 mm apart (parallel faces), B2-B3 touching, B1-B3 0.5 mm apart (a real gap)
        value = {"pairs": [{"a": 1, "b": 2, "gap_mm": 0.02, "parallel": True},
                           {"a": 2, "b": 3, "gap_mm": 0.0, "parallel": False, "overlap_mm3": 0.0},
                           {"a": 1, "b": 3, "gap_mm": 0.5, "parallel": False}],
                 "volumes": {"1": 10.0, "2": 5.0, "3": 50.0}, "truncated": ""}
        return type("R", (), {"ok": True, "value": value})()


class _Judge:
    def __init__(self, after: Any) -> None:
        self.after, self.calls = after, 0

    def full(self, pool: Any, ws: Any, original: str) -> Any:
        self.calls += 1
        return self.after


def _verdict(hard: tuple = (), kinds: tuple = (), clean: bool = False) -> Any:
    return type("V", (), {"valid": type("R", (), {"hard": [type("F", (), {"code": c})() for c in hard]})(),
                          "panel": [type("M", (), {"name": f"m{i}", "status": "fail", "error_kind": k})()
                                    for i, k in enumerate(kinds)],
                          "clean": clean, "inconclusive": False})()


def _run(monkeypatch: pytest.MonkeyPatch, *, after: Any, guard: Any = None, decline: set | None = None) -> Any:
    tweak, conf = _Op("tweak"), _Op("conformalise", decline=decline or set())
    monkeypatch.setitem(REGISTRY, "tweak", tweak)
    monkeypatch.setitem(REGISTRY, "conformalise", conf)
    judge = _Judge(after)
    lines: list[str] = []
    cp = C.ConformalPass(judge=judge, original="o.step", guard=guard or (lambda *a: Guarded(None, 0.0)))
    got = cp(_Pool(), _ws(), _verdict(kinds=("edge_through_face",)), counters=seq.SeqCounters(), step=lines.append)
    return got, cp, tweak, conf, judge, lines


def test_the_pass_tweaks_then_imprints_at_the_first_fuzzy_step_that_applies_and_is_kept(
        monkeypatch: pytest.MonkeyPatch) -> None:
    after = _verdict()
    got, cp, tweak, conf, judge, lines = _run(monkeypatch, after=after, decline={1e-7})
    assert tweak.calls == [(["B2", "B1"], {"max_mm": pytest.approx(0.1)})]       # the smaller body B2 moves
    # 1e-7 declined, then 0.022 (= 0.02 x 1.1) applied; the 0.5 mm real gap caps the schedule; the moved B2 is B9
    assert [p["fuzzy_mm"] for _, p in conf.calls] == pytest.approx([1e-7, 0.022])
    assert sorted(conf.calls[-1][0]) == ["B1", "B3", "B9"]
    assert got is not None and got[1] is after and judge.calls == 1
    assert [m.result for m in cp.log.records] == ["survived", "not_applied", "survived", "won"]
    assert {m.tier for m in cp.log.records} == {"conformal_pass"}
    assert cp.log.records[-1].deviation_mm == pytest.approx(0.02)
    assert any("conformal pass -> KEPT" in line for line in lines)


def test_the_pass_is_reverted_and_never_judged_when_the_loss_guard_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    got, cp, _t, _c, judge, lines = _run(monkeypatch, after=_verdict(),
                                         guard=lambda *a: Guarded("catastrophic_loss: lost B3", 0.9))
    assert got is None and judge.calls == 0
    assert cp.log.records[-1].result == "guard_refused" and cp.log.records[-1].reason_key == "catastrophic_loss"
    assert any("conformal pass -> reverted: catastrophic_loss" in line for line in lines)


def test_the_pass_is_reverted_when_the_targeted_failures_do_not_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    got, cp, *_ = _run(monkeypatch, after=_verdict(kinds=("edge_through_face",)))
    assert got is None and cp.log.records[-1].result == "still_there"


def test_a_single_body_or_a_part_with_nothing_targeted_is_not_measured() -> None:
    lines: list[str] = []
    cp = C.ConformalPass(judge=_Judge(None), original="o.step")
    assert cp(None, _ws(("B1",)), _verdict(kinds=("edge_through_face",)), counters=seq.SeqCounters(),
              step=lines.append) is None
    assert cp(None, _ws(), _verdict(hard=("open_shell",)), counters=seq.SeqCounters(), step=lines.append) is None
    assert lines == ["conformal pass: not run (no body_overlap or crossing mesher failure to judge it on)"]
