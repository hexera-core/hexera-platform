# Responsibility: Prove the staged healer's TRANSACTION RULE and stage order without a kernel: a
#                 stage that lowers the score is rolled back (and the workspace is the one it
#                 started with), a stage that does not is kept, and an inconclusive verdict or an
#                 op that declines/raises is neither a rollback nor a crash.
# Boundaries: hermetic. Ops are fakes registered in `ops.REGISTRY`; the judgement is a table keyed
#             by the state a fake op moved the workspace to, so a "good" and a "damaging" op differ
#             ONLY in the score they earn. What the real ops and checker do is proven natively in
#             tests/native/test_native_repair_v4_stages.py.
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from meshpipeline.cad.repair_v4 import ops, stages
from meshpipeline.cad.repair_v4.ops import OpDeclined, OpFailed
from meshpipeline.cad.repair_v4.verdict.assemble import InconclusiveVerdict
from meshpipeline.cad.repair_v4.workspace import Diff, Workspace

SCORES: dict[str, Any] = {"s0": (1, 1, 0.3, 0.0, 0)}


def _diff(op: str) -> Diff:
    return Diff(op=op, params={}, modified={}, generated={}, removed=[], declared_added=[],
                declared_removed=[], seconds=0.0)


class FakeOp:
    """Moves the workspace to `to` (a state with an entry in SCORES), or raises `raises`."""

    def __init__(self, name: str, to: str | None = None, raises: BaseException | None = None) -> None:
        self.name, self.to, self.raises = name, to, raises
        self.params_schema: dict = {"type": "object"}
        self.calls = 0

    def applies_to(self, site):  # pragma: no cover - unused
        return True

    def apply(self, pool, ws, targets, params, *, timeout_s):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        new = ws.fork()
        new.brep_path = self.to
        new.history.append(_diff(self.name))
        return new, new.history[-1]


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(ops, "validate_call", lambda *a, **k: [])
    monkeypatch.setattr(stages, "diagonal_mm", lambda pool, ws: 100.0)  # the default sew ladder is sized per part
    monkeypatch.setattr(ops, "REGISTRY", dict(ops.REGISTRY))
    judged: list[str] = []

    def judge(pool, ws):
        judged.append(ws.brep_path)
        value = SCORES[ws.brep_path]
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(stages, "_judge", judge)
    return SimpleNamespace(judged=judged, ops=ops.REGISTRY)


def _ws() -> Workspace:
    return Workspace(brep_path="s0", ids={"F1": "a", "F2": "b", "B1": "c"}, labels={}, history=[],
                     original="s0", indices={"F1": 1, "F2": 2, "B1": 1})


def test_order_matches_spec():
    assert [s.name for s in stages.STAGES] == [
        "baseline", "normalise", "edges", "faces", "shells", "solids", "bodies", "simplify",
        "condition", "finalise"]
    assert [s.name for s in stages.STAGES].index("simplify") < [s.name for s in stages.STAGES].index("condition")


def test_condition_defaults_are_the_specified_ones():
    d = stages.DEFAULTS["condition"]
    assert (d["angle_deg"], d["closed"], d["continuity"], d["proximity_ratio"]) == (90.0, True, 1, 0.25)


def test_simplify_keeps_no_split_edges(world):
    seen = {}

    class Spy(FakeOp):
        def apply(self, pool, ws, targets, params, *, timeout_s):
            seen.update(params)
            return super().apply(pool, ws, targets, params, timeout_s=timeout_s)

    SCORES["s_ok"] = (1, 1, 0.3, 0.0, 0)
    world.ops["unify_same_domain"] = Spy("unify_same_domain", to="s_ok")
    stages.run_stage(None, _ws(), "simplify")
    assert seen == {"scope": "all", "keep": []}


def test_stage_rolled_back_when_score_drops(world):
    SCORES["s_bad"] = (0, 0, 0.0, 0.0, 0)  # the stub stage breaks validity
    world.ops["to_canonical"] = FakeOp("to_canonical", to="s_bad")
    start = _ws()
    res = stages.run_stage(None, start, "faces")
    assert res.rolled_back == ["faces"] and res.kept == [] and res.inconclusive == []
    assert res.ws is start and res.ws.brep_path == "s0" and res.ws.history == []
    assert res.verdicts["faces"] == SCORES["s_bad"]  # the score it was judged (and lost) on
    assert world.ops["to_canonical"].calls == 1  # it did run; the rule undid it


def test_stage_kept_when_score_does_not_drop(world):
    SCORES["s_same"] = SCORES["s0"]
    SCORES["s_better"] = (1, 2, 0.4, 0.0, 0)
    for state, better in (("s_same", False), ("s_better", True)):
        world.ops["to_canonical"] = FakeOp("to_canonical", to=state)
        res = stages.run_stage(None, _ws(), "faces")
        assert res.kept == ["faces"] and res.rolled_back == []
        assert res.ws.brep_path == state and res.ws.history[-1].op == "to_canonical"
        assert (res.verdicts["faces"] > SCORES["s0"]) is better


def test_rollback_does_not_poison_later_stages(world):
    """A damaging stage is undone; the stages after it are judged against the state that stands."""
    SCORES["s_bad"] = (0, 0, 0.0, 0.0, 0)
    SCORES["s_good"] = (1, 2, 0.4, 0.0, 0)
    world.ops["tighten_tolerances"] = FakeOp("tighten_tolerances", to="s_bad")
    world.ops["unify_same_domain"] = FakeOp("unify_same_domain", to="s_good")
    res = stages.run_staged(None, _ws())
    assert res.rolled_back == ["normalise"] and "simplify" in res.kept
    assert res.ws.brep_path == "s_good"
    assert [d.op for d in res.ws.history] == ["unify_same_domain"]  # the damaging op left no trace


def test_inconclusive_verdict_is_not_a_regression(world):
    SCORES["s_flaky"] = InconclusiveVerdict("pool timeout")
    world.ops["to_canonical"] = FakeOp("to_canonical", to="s_flaky")
    start = _ws()
    res = stages.run_stage(None, start, "faces")
    assert res.inconclusive == ["faces"] and res.rolled_back == [] and res.kept == []
    assert res.ws is start  # an unjudged change is never kept ...
    assert world.judged.count("s_flaky") == stages.JUDGE_ATTEMPTS  # ... after the retry


def test_inconclusive_then_conclusive_is_judged_normally(world, monkeypatch):
    SCORES["s_ok"] = (1, 1, 0.3, 0.0, 0)
    world.ops["to_canonical"] = FakeOp("to_canonical", to="s_ok")
    answers = iter([InconclusiveVerdict("blip"), SCORES["s0"], SCORES["s_ok"]])

    def judge(pool, ws):
        a = next(answers)
        if isinstance(a, BaseException):
            raise a
        return a

    monkeypatch.setattr(stages, "_judge", judge)
    res = stages.run_stage(None, _ws(), "faces")
    assert res.kept == ["faces"] and res.inconclusive == []


def test_inconclusive_before_state_blocks_judgement(world):
    """If the standing state cannot be judged, no stage after it is kept on a guess."""
    SCORES["s_inc"] = InconclusiveVerdict("x")
    SCORES["s_ok"] = (1, 9, 0.9, 0.0, 0)
    world.ops["to_canonical"] = FakeOp("to_canonical", to="s_ok")
    start = _ws()
    start.brep_path = "s_inc"
    res = stages.run_stage(None, start, "faces")
    assert res.inconclusive == ["faces"] and res.ws is start


def test_checker_unavailable_is_inconclusive_too(world):
    from meshpipeline.cad.repair_v4.verdict.valid import CheckerUnavailable
    SCORES["s_down"] = CheckerUnavailable("no worker")
    world.ops["to_canonical"] = FakeOp("to_canonical", to="s_down")
    res = stages.run_stage(None, _ws(), "faces")
    assert res.inconclusive == ["faces"] and res.rolled_back == []


@pytest.mark.parametrize("exc", [OpDeclined("no_change"), OpFailed("timeout", "slow"),
                                 RuntimeError("kernel blew up"), ValueError("bad call")])
def test_op_trouble_is_a_skip_not_a_crash_or_rollback(world, exc):
    world.ops["to_canonical"] = FakeOp("to_canonical", raises=exc)
    start = _ws()
    res = stages.run_stage(None, start, "faces")
    assert res.ws is start and res.kept == [] and res.rolled_back == [] and "faces" in res.skipped
    assert world.judged == []  # nothing changed, so nothing was judged


def test_sew_without_make_volume_is_never_left_standing(world):
    """sew destroys a solid: if make_volume then fails, the sewn state must be discarded."""
    SCORES["s_sewn"] = (1, 5, 0.9, 0.0, 0)  # would even score higher; must still not stand
    world.ops["sew"] = FakeOp("sew", to="s_sewn")
    world.ops["make_volume"] = FakeOp("make_volume", raises=OpDeclined("no_closed_shell"))
    start = _ws()
    start.ids.pop("B1")
    res = stages.run_stage(None, start, "shells")
    assert res.ws is start and res.kept == [] and "shells" in res.skipped


def test_sew_then_make_volume_runs_as_one_stage(world):
    SCORES["s_sewn"] = (0, 0, 0.0, 0.0, 0)
    SCORES["s_solid"] = (1, 2, 0.4, 0.0, 0)
    world.ops["sew"] = FakeOp("sew", to="s_sewn")
    world.ops["make_volume"] = FakeOp("make_volume", to="s_solid")
    start = _ws()
    start.ids.pop("B1")
    res = stages.run_stage(None, start, "shells")
    assert res.kept == ["shells"] and [d.op for d in res.ws.history] == ["sew", "make_volume"]
    assert "s_sewn" not in world.judged  # only the finished unit was judged


def test_shells_orients_instead_of_sewing_when_a_solid_exists(world):
    SCORES["s_ok"] = (1, 1, 0.3, 0.0, 0)
    sew = world.ops["sew"] = FakeOp("sew", to="s_ok")
    world.ops["orient_and_close"] = FakeOp("orient_and_close", to="s_ok")
    stages.run_stage(None, _ws(), "shells")
    assert sew.calls == 0


def test_bodies_needs_two_solids(world):
    SCORES["s_ok"] = (1, 1, 0.3, 0.0, 0)
    conf = world.ops["conformalise"] = FakeOp("conformalise", to="s_ok")
    stages.run_stage(None, _ws(), "bodies")
    assert conf.calls == 0
    two = _ws()
    two.ids["B2"] = "d"
    stages.run_stage(None, two, "bodies")
    assert conf.calls == 1


def test_baseline_is_judged_once_and_reused(world):
    res = stages.run_staged(None, _ws())
    assert res.kept == ["baseline"] and world.judged == ["s0"]
    assert res.verdicts["baseline"] == SCORES["s0"]


def test_unknown_stage_and_param_names_are_refused(world):
    with pytest.raises(ValueError):
        stages.run_stage(None, _ws(), "polish")
    with pytest.raises(ValueError):
        stages.run_staged(None, _ws(), params={"polish": {}})


def test_judge_hands_an_inconclusive_verdict_to_score(monkeypatch):
    """The real `_judge` leaves the refusal to `score`; `_judged` then reports None, not a number."""
    monkeypatch.setattr(stages, "step_for", lambda pool, brep: brep)
    monkeypatch.setattr(stages, "original_facts", lambda pool, brep: (6, 1))
    monkeypatch.setattr(stages, "cheap_verdict", lambda *a, **k: _inconclusive())
    with pytest.raises(InconclusiveVerdict):
        stages._judge(None, _ws())
    assert stages._judged(None, _ws()) is None


# --- review round: what each stage passes, declared edits, solid-less gate, dead checker --------


def test_condition_passes_the_specified_values_to_the_ops(world):
    seen: dict[str, dict] = {}

    class Spy(FakeOp):
        def apply(self, pool, ws, targets, params, *, timeout_s):
            seen[self.name] = {"params": dict(params), "targets": list(targets)}
            return super().apply(pool, ws, targets, params, timeout_s=timeout_s)

    for name in ("split_angle", "split_closed", "split_continuity", "split_by_proximity"):
        world.ops[name] = Spy(name, to="s0")
    start = _ws()
    start.ids["F3"] = "x"
    stages.run_stage(None, start, "condition", {"face_pairs": [["F1", "F2"]]})
    assert seen["split_angle"]["params"] == {"max_deg": 90.0}
    assert seen["split_angle"]["targets"] == ["F1", "F2", "F3"]
    assert seen["split_closed"]["params"] == {}
    assert seen["split_continuity"]["params"] == {"order": 1}
    assert seen["split_by_proximity"]["params"] == {"face_pairs": [["F1", "F2"]], "target_ratio": 0.25}


def test_stage_run_is_the_path_the_transaction_uses(world):
    SCORES["s_ok"] = (1, 1, 0.3, 0.0, 0)
    world.ops["to_canonical"] = FakeOp("to_canonical", to="s_ok")
    stage = next(s for s in stages.STAGES if s.name == "faces")
    new, diffs = stage.run(None, _ws(), {})
    assert new.brep_path == "s_ok" and [d.op for d in diffs] == ["to_canonical"]
    res = stages.run_stage(None, _ws(), "faces")
    assert res.ws.brep_path == new.brep_path


def test_a_decline_reported_as_a_diff_is_not_a_change(world):
    """conformalise answers a decline with an unchanged workspace and params['declined']."""

    class Declining(FakeOp):
        def apply(self, pool, ws, targets, params, *, timeout_s):
            diff = _diff(self.name)
            diff.params = {"declined": "unresolved_overlap"}
            return ws, diff

    world.ops["conformalise"] = Declining("conformalise")
    two = _ws()
    two.ids["B2"] = "d"
    res = stages.run_stage(None, two, "bodies")
    assert res.kept == [] and res.rolled_back == [] and "unresolved_overlap" in res.skipped["bodies"]


def test_a_dead_checker_raises_rather_than_looking_like_no_progress(world):
    from meshpipeline.cad.repair_v4.verdict.valid import CheckerUnavailable
    SCORES["s_dead"] = CheckerUnavailable("down")
    start = _ws()
    start.brep_path = "s_dead"
    with pytest.raises(CheckerUnavailable):
        stages.run_staged(None, start)


def test_declared_edits_are_translated_to_face_map_indices():
    ws = Workspace(brep_path="n", ids={"F2": "a", "F9": "b", "F10": "c"}, labels={}, history=[],
                   original="o", indices={"F2": 4, "F9": 7, "F10": 8})
    fill = _diff("fill_loop")
    fill.declared_added = ["F9"]
    refit = _diff("refit_cluster")
    refit.declared_added = ["F8"]            # later replaced by F10 (Modified 1:N)
    refit.declared_removed = ["F1", "F3", "F12"]   # F12 was minted after the original's 6 faces
    refit.modified = {}
    later = _diff("split")
    later.modified = {"F8": ["F10"]}
    ws.history = [fill, refit, later]
    d = stages.declared_edits(ws, 6)
    assert d.added == ["F7", "F8"]           # indices in the REPAIRED shape
    assert d.removed == ["F1", "F3"]         # indices in the ORIGINAL; F12 has none to exempt


def _verdict(ok: bool, reasons: list[str], solids: int = 1):
    from meshpipeline.cad.repair_v4.verdict.assemble import Verdict
    from meshpipeline.cad.repair_v4.verdict.faithful import FaithfulReport
    from meshpipeline.cad.repair_v4.verdict.valid import ValidReport
    valid = ValidReport(ok, [], [], "mm", solids, ok)
    faith = FaithfulReport(not reasons, 0.0, 0.05, 1.0, 1.0, 1, 1, reasons=reasons) if ok else None
    return Verdict(valid, [SimpleNamespace(status="pass", min_sicn=0.3)], faith, False, "cheap")


def test_judge_threads_declared_edits_into_layer_three(monkeypatch):
    seen = {}

    def fake_faithful(pool, original, repaired, *, declared_added, declared_removed):
        seen.update(added=declared_added, removed=declared_removed, paths=(original, repaired))
        from meshpipeline.cad.repair_v4.verdict.faithful import FaithfulReport
        return FaithfulReport(True, 0.0, 0.05, 1.0, 1.0, 1, 1, declared_added, declared_removed)

    monkeypatch.setattr(stages, "step_for", lambda pool, brep: brep)
    monkeypatch.setattr(stages, "original_facts", lambda pool, brep: (6, 1))
    monkeypatch.setattr(stages, "cheap_verdict", lambda *a, **k: _verdict(True, ["deviation_exceeds_bound"]))
    monkeypatch.setattr(stages, "check_faithful", fake_faithful)
    ws = Workspace(brep_path="n", ids={"F9": "a"}, labels={}, history=[], original="o", indices={"F9": 7})
    d = _diff("fill_loop")
    d.declared_added, d.declared_removed = ["F9"], ["F2"]
    ws.history = [d]
    out = stages._judge(None, ws)
    assert seen == {"added": ["F7"], "removed": ["F2"], "paths": ("o", "n")}
    assert out[0] == 1  # the undeclared verdict said unfaithful; declared, it is faithful
    assert out[4] == -2  # and churn is no longer permanently 0


def test_solidless_original_gates_on_validity_and_the_two_expected_reasons(monkeypatch):
    monkeypatch.setattr(stages, "step_for", lambda pool, brep: brep)
    monkeypatch.setattr(stages, "original_facts", lambda pool, brep: (6, 0))
    ws = Workspace(brep_path="n", ids={}, labels={}, history=[], original="o")
    expected = ["volume_changed", "body_count_changed"]
    cases = [(True, expected, 1),                       # a correct heal of loose faces
             (False, [], 0),                            # BRepCheck-invalid: must NOT pass on equal SICN
             (True, expected + ["area_changed"], 0),
             (True, expected + ["deviation_exceeds_bound"], 0)]
    for ok, reasons, want in cases:
        monkeypatch.setattr(stages, "cheap_verdict", lambda *a, _v=_verdict(ok, reasons), **k: _v)
        assert stages._judge(None, ws)[0] == want, (ok, reasons)


def _inconclusive():
    from dataclasses import replace
    return replace(_verdict(True, []), inconclusive=True)


def test_original_facts_are_re_read_when_a_reused_path_holds_a_different_file(tmp_path):
    brep = tmp_path / "orig.brep"
    brep.write_bytes(b"one")
    n = {"calls": 0}

    class Pool:
        def call(self, fn, payload, *, timeout_s):
            n["calls"] += 1
            faces = 3 if n["calls"] == 1 else 5
            ents = [{"type": "face", "index": k} for k in range(1, faces + 1)]
            return SimpleNamespace(ok=True, value={"entities": ents}, status="ok", error=None)

    assert stages.original_facts(Pool(), str(brep)) == (3, 0)
    assert stages.original_facts(Pool(), str(brep)) == (3, 0) and n["calls"] == 1   # cached while unchanged
    brep.write_bytes(b"two!")
    import os
    os.utime(brep, (1_000_000_000, 1_000_000_000))                                   # the path is reused, the file is not
    assert stages.original_facts(Pool(), str(brep)) == (5, 0) and n["calls"] == 2
