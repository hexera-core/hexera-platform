# Responsibility: B20 - the candidate cap and pruning. Aimed candidates rank first WITHIN a family; a candidate that
#                 provably cannot reach a new state is dropped BEFORE any verdict; and the verdicts saved are
#                 counted, because that is the number that buys wall clock.
# Boundaries: hermetic. The ordering and the prune, through the real `search`, with a fake judge and fake ops.
from __future__ import annotations

import itertools
from typing import Any

from meshpipeline.cad.repair_v4 import search as S
from meshpipeline.cad.repair_v4.ops import REGISTRY, OpDeclined
from meshpipeline.cad.repair_v4.types import Site
from meshpipeline.cad.repair_v4.workspace import Diff, Workspace

_n = itertools.count(1)


def _ws(q: int = 0) -> Workspace:
    i = next(_n)
    return Workspace(brep_path=f"/fake/{i}.brep", ids={"F1": "a", "F2": "b", "B1": "c", "B2": "d"},
                     labels={"_q": {"q": q}}, history=[], original="/fake/orig.brep",
                     indices={"F1": 1, "F2": 2, "B1": 1, "B2": 2})


def site(kind: str, entities: list[str], **graph: Any) -> Site:
    return Site(id="S1", kind=kind, entities=entities, xyz=[], meshers_failing=["gmsh_prod"],
                dist_over_h=None, graph=graph, view_png=None)


# ---- (1) aimedness orders WITHIN a family ------------------------------------------------------

def test_a_measured_target_outranks_the_site_at_large_and_a_guess() -> None:
    s = site("self_overlap", ["F1", "F2"], mesher_faces=["F1"],
             clashes=[{"a": "F1", "b": "F2", "touching": False}])
    assert S._aimedness(s, "split_angle", ["F1"], {}) == S.AIMED_MEASURED
    assert S._aimedness(s, "split_angle", ["F1", "F2"], {}) == S.AIMED_SITE
    assert S._aimedness(s, "split_by_proximity", [], {"face_pairs": [["F1", "F2"]]}) == S.AIMED_MEASURED
    assert S._aimedness(s, "split_by_proximity", [], {"face_pairs": [["F2", "B1"]]}) == S.AIMED_GUESSED
    assert S._aimedness(s, "unify_same_domain", [], {"scope": "all"}) == S.AIMED_GUESSED


def test_the_located_face_is_proposed_before_the_sites_whole_face_set() -> None:
    gen = S.DetGenerator(diagonal=lambda _w: 100.0)
    st = S.SearchState(id="S0", ws=_ws(), verdict=None, score=(0, 0, 0.0, 0.0, 0),
                       sites=[site("self_overlap", ["F1", "F2", "B1"], mesher_faces=["F2"])],
                       depth=0, parent=None, via=None)
    angles = [list(c.targets) for c in gen.propose(st, 50) if c.op == "split_angle"]
    assert angles, "no split_angle proposed"
    assert angles[0][0] == "F2", angles          # the face the mesher LOCATED leads the aimed row
    assert any(set(t) == {"F1", "F2"} for t in angles), angles   # the site's wider set is still offered, behind


# ---- (3) pairs the kernel could not measure ----------------------------------------------------

def test_no_pair_row_is_built_from_combinations_when_the_kernel_measured_nothing() -> None:
    gen = S.DetGenerator(diagonal=lambda _w: 100.0)
    st = S.SearchState(id="S0", ws=_ws(), verdict=None, score=(0, 0, 0.0, 0.0, 0),
                       sites=[site("proximity", ["F1", "F2"])], depth=0, parent=None, via=None)
    got = gen.propose(st, 50)
    assert [c for c in got if c.op == "split_by_proximity"] == []
    assert gen.skipped["no_measured_pair"] == 1
    assert any("measured no near pair" in n for n in gen.notes), gen.notes


def test_the_guess_is_available_behind_a_flag_and_only_where_the_kernel_FAILED_to_measure() -> None:
    st = S.SearchState(id="S0", ws=_ws(), verdict=None, score=(0, 0, 0.0, 0.0, 0),
                       sites=[site("proximity", ["F1", "F2"], unmeasured_pairs=3)],
                       depth=0, parent=None, via=None)
    off = S.DetGenerator(diagonal=lambda _w: 100.0, guess_pairs=False)
    assert [c for c in off.propose(st, 50) if c.op == "split_by_proximity"] == []
    on = S.DetGenerator(diagonal=lambda _w: 100.0, guess_pairs=True)
    guessed = [c for c in on.propose(st, 50) if c.op == "split_by_proximity"]
    assert guessed, "the flag must restore the guess where the kernel could not measure"
    assert any("GUESSED pairs" in n for n in on.notes), on.notes
    # and with the flag on but NOTHING unmeasured, there is still nothing to guess at
    measured_all = S.SearchState(id="S0", ws=_ws(), verdict=None, score=(0, 0, 0.0, 0.0, 0),
                                 sites=[site("proximity", ["F1", "F2"])], depth=0, parent=None, via=None)
    assert [c for c in on.propose(measured_all, 50) if c.op == "split_by_proximity"] == []


# ---- (2) the prune, through the real search -----------------------------------------------------

class _NoChangeOp:
    """An op that declines `no_change` whatever it is given: the C10 shape (11 no-op calls on one file)."""

    name = "b20_no_change"
    params_schema = {"type": "object", "properties": {"fuzzy_mm": {"type": "number"}},
                     "additionalProperties": False, "x-targets": {"kind": "face", "min": 0}}
    calls = 0

    def applies_to(self, site: Site) -> bool:
        return True

    def apply(self, pool: Any, ws: Workspace, targets: list[str], params: dict, *, timeout_s: float):
        type(self).calls += 1
        raise OpDeclined("no_change: there was nothing to change here")


class _Judge:
    def __init__(self) -> None:
        self.cheap_calls = 0

    def judge(self) -> S.Judge:
        return S.Judge(cheap=self._cheap, full=self._full, score=self._score, sites=self._sites)

    def _cheap(self, pool: Any, ws: Workspace, original: str) -> Any:
        self.cheap_calls += 1
        return _V(0, False, "cheap")

    def _full(self, pool: Any, ws: Workspace, original: str) -> Any:
        return _V(0, False, "full")

    @staticmethod
    def _score(v: Any) -> tuple:
        return (1, v.q, 0.0, 0.0, 0)

    @staticmethod
    def _sites(pool: Any, ws: Workspace, verdict: Any) -> list[Site]:
        return [site("invalid", ["F1"])]


class _V:
    def __init__(self, q: int, clean: bool, tier: str) -> None:
        self.q, self.clean, self.tier = q, clean, tier
        self.inconclusive = False


class _Same:
    """Proposes the SAME candidate at every state, so the duplicate rule has something to catch."""

    def __init__(self, op: str) -> None:
        self.op = op

    def propose(self, state: S.SearchState, k: int) -> list[S.Candidate]:
        return [S.Candidate(self.op, ["F1"], {}, "det", "the same call again")]


class _OkThenSame:
    """One op that APPLIES (so there is a child) and one that declines `no_change` on the same targets with a
    DIFFERENT parameter every time - C10's real shape, 11 no-op conformalise calls differing only in fuzz. The
    duplicate rule cannot catch those; only the `no_change` rule can, because it keys on the op and WHERE."""

    def __init__(self) -> None:
        self.n = 0

    def propose(self, state: S.SearchState, k: int) -> list[S.Candidate]:
        self.n += 1
        return [S.Candidate("b20_ok", ["F2"], {"step": self.n}, "det", "apply, so the search expands"),
                S.Candidate("b20_no_change", ["F1"], {"fuzzy_mm": self.n / 1000}, "det",
                            f"declines no_change on F1 (fuzz {self.n})")]


def test_an_op_that_declined_no_change_is_never_run_again_on_the_same_targets(monkeypatch) -> None:
    monkeypatch.setitem(REGISTRY, "b20_no_change", _NoChangeOp())
    monkeypatch.setitem(REGISTRY, "b20_ok", _OkOp())
    _NoChangeOp.calls = _OkOp.calls = 0
    judge = _Judge()
    start = _ws()
    got = S.search(None, start, start.original, [_OkThenSame()],
                   S.Budget(wall_s=1000, ops=20, model_calls=0, model_usd=0.0),
                   workers=1, judge=judge.judge(), patience=3)
    assert _OkOp.calls >= 1, "the applying op must have produced a child to expand"
    assert _NoChangeOp.calls == 1, f"the op ran {_NoChangeOp.calls} times; C10's waste was 11"
    assert got.pruned.get("no_change_on_these_targets_already", 0) >= 1, got.pruned
    assert got.verdicts_saved == sum(got.pruned.values()) >= 1


class _OkOp:
    """An op that APPLIES and reaches a new state each time, so the search expands and the same candidate can be
    proposed again at the child - which is where the duplicate rule has to catch it."""

    name = "b20_ok"
    params_schema = {"type": "object", "properties": {"step": {"type": "integer"}},
                     "additionalProperties": False, "x-targets": {"kind": "face", "min": 0}}
    calls = 0

    def applies_to(self, site: Site) -> bool:
        return True

    def apply(self, pool: Any, ws: Workspace, targets: list[str], params: dict, *, timeout_s: float):
        type(self).calls += 1
        fresh = Workspace(brep_path=f"/fake/ok{type(self).calls}.brep",
                          ids={k: f"{v}{type(self).calls}" for k, v in ws.ids.items()},
                          labels=dict(ws.labels), history=list(ws.history), original=ws.original,
                          indices=dict(ws.indices))
        return fresh, Diff(op=self.name, params={}, modified={}, generated={}, removed=[], declared_added=[],
                           declared_removed=[], seconds=0.0)


def test_an_exact_duplicate_already_run_on_this_lineage_costs_no_second_verdict(monkeypatch) -> None:
    monkeypatch.setitem(REGISTRY, "b20_ok", _OkOp())
    _OkOp.calls = 0
    judge = _Judge()
    start = _ws()
    got = S.search(None, start, start.original, [_Same("b20_ok")],
                   S.Budget(wall_s=1000, ops=20, model_calls=0, model_usd=0.0),
                   workers=1, judge=judge.judge(), patience=3)
    # the op runs ONCE per lineage: the child is offered the same call and it is dropped before any verdict
    assert _OkOp.calls == 1, f"the op ran {_OkOp.calls} times"
    assert got.pruned.get("already_run_on_this_lineage", 0) >= 1, got.pruned
    # the saving is REPORTED as verdicts, which is what buys wall clock
    assert got.verdicts_saved >= 1
    assert [e for e in got.trace.of_kind("candidate") if e.get("outcome") == "pruned"], "the drop must be traceable"


def test_the_prune_reports_itself_on_the_row(monkeypatch) -> None:
    """`arms.search_triage` carries pruned/verdicts_saved so a wave can say what the rules bought."""
    import sys
    from pathlib import Path

    where = Path(__file__).parents[4] / "devtools/validation/v4_eval"
    if str(where) not in sys.path:
        sys.path.insert(0, str(where))
    import arms
    monkeypatch.setitem(REGISTRY, "b20_ok", _OkOp())
    judge = _Judge()
    start = _ws()
    got = S.search(None, start, start.original, [_Same("b20_ok")],
                   S.Budget(wall_s=1000, ops=20, model_calls=0, model_usd=0.0),
                   workers=1, judge=judge.judge(), patience=3)
    triage = arms.search_triage(got, [])
    assert triage["verdicts_saved"] == got.verdicts_saved
    assert triage["pruned"] == got.pruned
