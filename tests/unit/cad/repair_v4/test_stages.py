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


def test_a_transaction_is_judged_on_validity_alone_whatever_layer_3_reports(monkeypatch):
    """B12: the staged keep rule's first key is VALID; no layer-3 reason (area, deviation, volume, body count) can sink
    a stage any more, for a solid-less original or any other. (It used to gate on the two expected reasons.)"""
    monkeypatch.setattr(stages, "step_for", lambda pool, brep: brep)
    for solids in (0, 1):
        monkeypatch.setattr(stages, "original_facts", lambda pool, brep, _s=solids: (6, _s))
        ws = Workspace(brep_path="n", ids={}, labels={}, history=[], original="o")
        expected = ["volume_changed", "body_count_changed"]
        cases = [(True, expected, 1),
                 (False, [], 0),                            # BRepCheck-invalid still scores 0
                 (True, expected + ["area_changed"], 1),
                 (True, expected + ["deviation_exceeds_bound"], 1),
                 (True, ["fill_too_large", "area_changed"], 1)]
        for ok, reasons, want in cases:
            monkeypatch.setattr(stages, "cheap_verdict", lambda *a, _v=_verdict(ok, reasons), **k: _v)
            assert stages._judge(None, ws)[0] == want, (solids, ok, reasons)


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


# --- Task 53: a stage says how many ops it applied, so the search can charge for them -----------


def test_run_stage_reports_how_many_ops_it_applied(world):
    """`shells` with no solid is a CHAIN (sew THEN make_volume): one stage, two ops. The count comes
    from the diffs the op library produced, not from a table of stage names."""
    SCORES["s_sewn"] = (1, 1, 0.3, 0.0, 0)
    SCORES["s_solid"] = (1, 2, 0.4, 0.0, 0)
    world.ops["sew"] = FakeOp("sew", to="s_sewn")
    world.ops["make_volume"] = FakeOp("make_volume", to="s_solid")
    start = _ws()
    start.ids.pop("B1")
    res = stages.run_stage(None, start, "shells")
    assert res.kept == ["shells"] and res.applied_ops == {"shells": 2}


def test_a_single_op_stage_reports_one(world):
    SCORES["s_ok"] = (1, 1, 0.3, 0.0, 0)
    world.ops["to_canonical"] = FakeOp("to_canonical", to="s_ok")
    res = stages.run_stage(None, _ws(), "faces")
    assert res.kept == ["faces"] and res.applied_ops == {"faces": 1}


def test_applied_ops_counts_the_work_of_a_rolled_back_stage_and_a_broken_chain(world):
    """The ops of a rolled-back stage, and of a chain abandoned halfway, really ran on the kernel.
    They are reported, so nobody gets that geometry work for free."""
    SCORES["s_bad"] = (0, 0, 0.0, 0.0, 0)
    world.ops["to_canonical"] = FakeOp("to_canonical", to="s_bad")
    rolled = stages.run_stage(None, _ws(), "faces")
    assert rolled.rolled_back == ["faces"] and rolled.applied_ops == {"faces": 1}

    SCORES["s_sewn"] = (1, 5, 0.9, 0.0, 0)
    world.ops["sew"] = FakeOp("sew", to="s_sewn")
    world.ops["make_volume"] = FakeOp("make_volume", raises=OpDeclined("no_closed_shell"))
    start = _ws()
    start.ids.pop("B1")
    broken = stages.run_stage(None, start, "shells")
    assert "shells" in broken.skipped and broken.applied_ops == {"shells": 1}


def test_a_stage_that_applied_nothing_reports_zero(world):
    world.ops["to_canonical"] = FakeOp("to_canonical", raises=OpDeclined("no_change"))
    res = stages.run_stage(None, _ws(), "faces")
    assert res.applied_ops == {"faces": 0}


# --- the FULL-panel confirmation of the staged result (Task 49: `condition` broke gmsh_prod on a clean file) ------


def _full(passes: int, of: int = 4, *, valid: bool = True, clean: bool | None = None) -> Any:
    panel = [SimpleNamespace(name=f"m{i}", status="pass" if i < passes else "fail") for i in range(of)]
    return SimpleNamespace(panel=panel, valid=SimpleNamespace(ok=valid), inconclusive=False,
                           clean=(passes == of and valid) if clean is None else clean)


class StepOp(FakeOp):
    """Moves `frm` -> `to` and refuses (an op error: noted, the stage carries on) from any other state, so one op
    name can be used by several stages and act in only the one the test means."""

    def __init__(self, name: str, frm: str, to: str) -> None:
        super().__init__(name, to=to)
        self.frm = frm

    def apply(self, pool, ws, targets, params, *, timeout_s):
        if ws.brep_path != self.frm:
            raise RuntimeError("not this state")
        return super().apply(pool, ws, targets, params, timeout_s=timeout_s)


def test_a_stage_that_breaks_a_panel_member_the_cheap_tier_cannot_see_is_dropped(world):
    """The smoke's regression, in miniature: a clean input; `condition` passes the cheap tier (gmsh_default) and
    `finalise` stands on it, but the FULL panel loses a member. The confirmation falls back stage by stage to the
    last state that passes as many members as the input - dropping `condition` and everything after it."""
    world.ops["tighten_tolerances"] = StepOp("tighten_tolerances", "s0", "n1")
    world.ops["split_angle"] = StepOp("split_angle", "n1", "c1")
    world.ops["orient_and_close"] = StepOp("orient_and_close", "c1", "f1")
    SCORES.update({"n1": (1, 1, 0.3, 0.0, 0), "c1": (1, 1, 0.6, 0.0, 0), "f1": (1, 1, 0.6, 0.0, 0)})
    full = {"s0": _full(4), "n1": _full(4), "c1": _full(3), "f1": _full(3)}
    res = stages.run_staged(None, _ws())
    assert res.ws.brep_path == "f1" and "condition" in res.kept and "finalise" in res.kept  # the cheap tier kept it
    conf = stages.confirm_staged(None, _ws(), res, full=lambda ws: full[ws.brep_path], input_verdict=full["s0"])
    assert res.ws.brep_path == "n1" and conf.fell_back_to == "normalise"
    assert conf.dropped == ["condition", "finalise"] and conf.verdict is full["n1"]
    assert res.confirmation is conf


def test_when_every_staged_state_regresses_the_input_stands(world):
    world.ops["tighten_tolerances"] = FakeOp("tighten_tolerances", to="n1")
    SCORES["n1"] = (1, 1, 0.3, 0.0, 0)
    full = {"s0": _full(4), "n1": _full(2)}
    start = _ws()
    res = stages.run_staged(None, start)
    conf = stages.confirm_staged(None, start, res, full=lambda ws: full[ws.brep_path], input_verdict=full["s0"])
    assert res.ws is start and conf.fell_back_to == "input" and "normalise" in conf.dropped


def test_a_confirmed_staged_result_is_left_alone_and_judged_once(world):
    world.ops["tighten_tolerances"] = FakeOp("tighten_tolerances", to="n1")
    SCORES["n1"] = (1, 1, 0.3, 0.0, 0)
    calls: list[str] = []

    def full(ws: Workspace) -> Any:
        calls.append(ws.brep_path)
        return _full(4)

    res = stages.run_staged(None, _ws())
    conf = stages.confirm_staged(None, _ws(), res, full=full, input_verdict=_full(3, clean=False))
    assert res.ws.brep_path == "n1" and conf.fell_back_to is None and conf.dropped == [] and calls == ["n1"]


def test_validity_and_cleanliness_count_as_well_as_panel_passes(world):
    world.ops["tighten_tolerances"] = FakeOp("tighten_tolerances", to="n1")
    SCORES["n1"] = (1, 1, 0.3, 0.0, 0)
    res = stages.run_staged(None, _ws())
    invalid = _full(4, valid=False, clean=False)                    # same passes, but no longer valid
    conf = stages.confirm_staged(None, _ws(), res, full=lambda ws: invalid if ws.brep_path == "n1" else _full(4),
                                 input_verdict=_full(4))
    assert conf.fell_back_to == "input"


def test_an_unchanged_staged_result_needs_no_confirmation(world):
    start = _ws()
    res = stages.StagedResult(ws=start)
    conf = stages.confirm_staged(None, start, res, full=lambda ws: pytest.fail("nothing changed: nothing to mesh"),
                                 input_verdict=_full(4))
    assert conf.fell_back_to is None and conf.verdict is None


# ------------------------------------------------------------------ A8: a rollback and a no-op SAY WHY

def _scored(valid, passes, sicn, members) -> Any:
    return stages.Scored((valid, passes, sicn, 0.0, 0), members)


def test_a_rollback_phrase_names_the_member_that_got_worse_and_by_how_much() -> None:
    before = _scored(1, 2, 0.30, {"gmsh_prod": {"status": "pass", "min_sicn": 0.30},
                                  "gmsh_default": {"status": "pass", "min_sicn": 0.50}})
    after = _scored(1, 1, 0.12, {"gmsh_prod": {"status": "fail", "min_sicn": None, "error_kind": "no_volume"},
                                 "gmsh_default": {"status": "pass", "min_sicn": 0.12}})
    why = stages.rollback_detail(before, after)
    assert "gmsh_prod pass -> fail (no_volume)" in why
    assert "gmsh_default min_sicn 0.5 -> 0.12" in why
    assert "panel_passes 2 -> 1" in why


def test_the_phrase_reports_a_worse_score_even_when_no_member_changed_status() -> None:
    m = {"gmsh_default": {"status": "pass", "min_sicn": 0.4}}
    before, after = _scored(1, 1, 0.4, m), _scored(0, 1, 0.4, m)
    assert "valid 1 -> 0" in stages.rollback_detail(before, after)
    # nothing to say is said plainly, never a fabricated cause
    assert stages.rollback_detail(_scored(1, 1, 0.4, m), _scored(1, 1, 0.4, m)) == "no measured difference"


def test_a_member_whose_sicn_vanished_without_a_status_change_does_not_crash_the_phrase() -> None:
    """D15: a fail -> fail member with min_sicn 0.3 -> None raised TypeError in the staged healer, which took the
    staged arm and both search arms down on 3 of 30 dev_v1r files."""
    before = _scored(1, 1, 0.3, {"gmsh_alt": {"status": "fail", "min_sicn": 0.3}})
    after = _scored(1, 1, 0.0, {"gmsh_alt": {"status": "fail", "min_sicn": None}})
    assert "gmsh_alt min_sicn 0.3 -> none" in stages.rollback_detail(before, after)

def test_the_phrase_is_empty_when_the_scores_carry_no_members() -> None:
    assert stages.rollback_detail((1, 1, 0.3, 0.0, 0), (1, 0, 0.3, 0.0, 0)) == "panel_passes 1 -> 0"


def test_a_stage_that_applied_nothing_says_what_it_looked_at(world) -> None:
    ws = Workspace(brep_path="s0", ids={"F1": "a", "B1": "c"}, labels={}, history=[], original="s0",
                   indices={"F1": 1, "B1": 1})
    result = stages.run_stage(None, ws, "bodies", {})
    assert "bodies" in result.skipped
    why = result.skipped["bodies"]
    assert why != "no_change" and "1 solid" in why and "2" in why  # what it looked at, and why nothing applied
    assert result.applied_ops["bodies"] == 0 and not result.rolled_back


def test_the_solidless_workaround_is_gone_because_layer_three_no_longer_gates(monkeypatch) -> None:
    """It existed only because score key 0 was "valid AND faithful": a solid-less original always reports
    volume_changed/body_count_changed, so a correct heal had to be exempted by hand. Key 0 is now layer 1 alone
    (owner decision 2026-11-24), so the exemption is dead code and the score is the plain one."""
    import re as _re

    from meshpipeline.cad.repair_v4 import stages as st

    src = __import__("pathlib").Path(st.__file__).read_text()
    assert "_SOLIDLESS_OK_REASONS" not in src
    body = src[src.index("def _judge("):src.index("def _judged(")]
    assert "solids == 0" not in body and _re.search(r"\(int\(gate\), \*result", body) is None


def test_c18_p1_layer_one_is_reused_only_when_the_gate_is_unchanged(monkeypatch):
    """P1: the standing state's layer-1 report is handed to the cheap verdict only when `layer1_gate` (A17's per-solid
    key + face and free-shell counts) is the same; any other gate, or none, re-runs layer 1."""
    seen: list = []
    monkeypatch.setattr(stages, "step_for", lambda pool, brep: brep)
    monkeypatch.setattr(stages, "original_facts", lambda pool, brep: (6, 1))
    monkeypatch.setattr(stages, "layer1_gate", lambda pool, brep: {"a": "G1", "b": "G1", "c": "G2"}.get(brep))

    def cheap(*a, **k):
        seen.append(k.get("valid_report"))
        return _verdict(True, [])

    monkeypatch.setattr(stages, "cheap_verdict", cheap)
    ws = lambda brep: Workspace(brep_path=brep, ids={}, labels={}, history=[], original="o")  # noqa: E731
    standing = stages._judge(None, ws("a"))
    assert seen == [None] and standing.gate == "G1" and not standing.reused_layer1
    same = stages._judge(None, ws("b"), reuse=standing)
    assert seen[-1] is standing.valid and same.reused_layer1
    for brep in ("c", "unknown"):  # a different gate, and a gate that could not be computed
        assert not stages._judge(None, ws(brep), reuse=standing).reused_layer1 and seen[-1] is None
