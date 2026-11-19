"""Tournament repair: rehearsing one candidate on its own clone, and K of them on one wall.

Every budget/threshold assertion here is a TABLE over values on BOTH sides of the constant. A
one-sided assertion has silently passed a mutated constant four times on this plan, so a single
`>` is not evidence.

The session is never reachable from a rehearsal: the only shape handed in is `session_shape`, and
the spy in `test_the_session_object_is_never_touched` asserts that nothing is written on it.
"""
from __future__ import annotations

import types

import pytest

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.cad.repair import tournament as T
from meshpipeline.cad.repair.caps import Measure
from meshpipeline.cad.repair.contracts import DefectCode, DefectSeverity
from meshpipeline.cad.repair.ladder import TierResult
from meshpipeline.cad.repair.localize import EntityDefect
from meshpipeline.cad.repair.package import PackageBudget, Predicate, TargetPackage
from meshpipeline.cad.repair.regions import Region

#: A LOCATED region with a real box: an unlocated one admits every record, so a scoping
#: assertion made against it would pass however the scope was computed.
REGION = Region(index=0, fingerprints=frozenset({"f1", "f2"}), bbox_min=(0.0, 0.0, 0.0),
                bbox_max=(10.0, 10.0, 10.0), face_indices=frozenset({1}), located=True)
PREDICATE = Predicate(cleared=frozenset({"f1"}), reclassified=frozenset(),
                      unchanged=frozenset(), max_area_delta_mm2=1.0, max_volume_delta_mm3=1.0)
BUDGET = PackageBudget(max_ops=3, timeout_s=10.0, model_calls=1, deviation_mm=0.5)
ORIGINAL = Measure(True, 1, 1, 6, 12, 8, 100.0, 1e-3, 600.0, 1000.0)


def defect(fingerprint: str) -> EntityDefect:
    """A record inside the region, or - for a name starting `out` - far outside its grown box."""
    far = fingerprint.startswith("out")
    lo = (5000.0, 5000.0, 5000.0) if far else (1.0, 1.0, 1.0)
    hi = (5001.0, 5001.0, 5001.0) if far else (2.0, 2.0, 2.0)
    return EntityDefect(code=DefectCode.open_shell, severity=DefectSeverity.error,
                        entity_type="face", entity_index=9 if far else 1, message="x",
                        fingerprint=fingerprint,
                        location={"bbox_min": lo, "bbox_max": hi})


def item(op="sew", fingerprint="f1"):
    return {"op": types.SimpleNamespace(name=op),
            "targets": (types.SimpleNamespace(fingerprint=fingerprint, targetable=True),),
            "defect_code": DefectCode.open_shell, "tolerance_mm": 0.01}


def cand(source=T.RULES, plan=None, intent="fix") -> T.Candidate:
    plan = [item()] if plan is None else plan
    return T.candidate(source, TargetPackage(region=REGION, intent=intent, plan=tuple(plan),
                                             predicate=PREDICATE, budget=BUDGET))


class FakeSession:
    """Just enough of `RepairSession` for `try_op`, and a record of every call."""

    def __init__(self, statuses, *, raises=False):
        self.statuses = list(statuses)
        self.raises = raises
        self.calls: list[tuple] = []

    def try_op(self, op, targets, defect_code, *, tolerance_mm, within=None, **kw):
        self.calls.append((op.name, tuple(t.fingerprint for t in targets), tolerance_mm, within))
        if self.raises:
            raise RuntimeError("the kernel fell over")
        status = self.statuses.pop(0) if self.statuses else "kept"
        return types.SimpleNamespace(op=op.name, status=status, reason="", apply_s=0.0,
                                     scan_s=0.0, measure_s=0.0)


class FakeScratch:
    def __init__(self, source, *, after, statuses, raises=False, **kw):
        self.source = source
        self.kwargs = kw
        self.session = FakeSession(statuses, raises=raises)
        self.shape = object()
        self._after = tuple(after)

    def edited_faces(self):
        return ("face:1",)

    def defects(self):
        return self._after


def harness(*, statuses=(), after=("f2",), before=("f1", "f2"), tiers=None, raises=False,
            deviation_mm=0.02, spend=False):
    """The injection kit. Returns (kwargs, scratches, tier_calls)."""
    scratches: list[FakeScratch] = []
    tier_calls: list[tuple] = []
    at = [1000.0]

    def make_scratch(source, **kw):
        s = FakeScratch(source, after=[defect(f) for f in after], statuses=statuses,
                        raises=raises, **kw)
        scratches.append(s)
        return s

    def run_tier(shape, tier, *, diagonal_mm, within=None, budget_s):
        tier_calls.append((tier, within, budget_s))
        passed = True if tiers is None else tiers.get(tier, True)
        return TierResult(str(tier), passed, 0.01)

    def measure(before_shape, after_shape, faces, *, budget_s):
        if spend:
            # The LAST thing a rehearsal does, so this says "the candidate used the whole
            # allowance it was given" and the wall really runs down across `rehearse_all`
            # instead of being handed out again untouched.
            at[0] += float(scratches[-1].kwargs["remaining_s"])
        return types.SimpleNamespace(mm=deviation_mm, seconds=0.0)

    # A clock shaped like `InvestigationClock` (which carries the run's own in `.clock`), driven
    # by the test: the boundary rows of the tables below are exact, and a real monotonic clock
    # would shave microseconds off a budget and make 15.0 read as 14.99998.
    kwargs = {"clock": types.SimpleNamespace(clock=lambda: at[0]),
              "session_shape": object(), "original": ORIGINAL,
              "defects": tuple(defect(f) for f in before), "region": REGION,
              "tolerance_mm": 0.01, "make_scratch": make_scratch, "run_tier": run_tier,
              "measure": measure}
    return kwargs, scratches, tier_calls


# --- the plan on the clone -------------------------------------------------------------------

def test_a_clean_plan_is_rehearsed_and_reports_its_kept_ops():
    kwargs, scratches, tiers = harness(statuses=["kept", "kept"])
    r = T.rehearse(cand(plan=[item(), item("fill", "f1")]), budget_s=30.0, **kwargs)
    assert (r.status, r.kept_ops) == (T.REHEARSED, 2)
    assert r.deviation_mm == 0.02
    assert [t[0] for t in tiers] == ["0", "1", "2"]
    assert len(scratches) == 1                         # ONE clone, and it is not the session
    assert scratches[0].source is kwargs["session_shape"]


@pytest.mark.parametrize("statuses,kept", [
    (["rolled_back"], 0),
    (["kept", "rolled_back"], 1),
    (["kept", "declined: no"], 1),
    (["failed"], 0),
])
def test_first_declined_item_fails_the_rehearsal(statuses, kept):
    """One failed item fails the whole package - the session's own rule, on the clone."""
    kwargs, scratches, tiers = harness(statuses=statuses)
    plan = [item(), item("fill", "f1"), item("sew", "f2")]
    r = T.rehearse(cand(plan=plan), budget_s=30.0, **kwargs)
    assert r.status == T.FAILED
    assert r.kept_ops == kept
    assert len(scratches[0].session.calls) == len(statuses)   # it STOPPED at the first no
    assert tiers == []                                        # and probed nothing
    assert statuses[-1].split(":")[0] in r.reason


def test_the_region_travels_into_every_operation():
    kwargs, scratches, _ = harness(statuses=["kept"])
    T.rehearse(cand(), budget_s=30.0, **kwargs)
    assert scratches[0].session.calls[0][3] is REGION
    assert scratches[0].session.calls[0][2] == 0.01


def test_rehearse_never_raises():
    kwargs, _, _ = harness(raises=True)
    r = T.rehearse(cand(), budget_s=30.0, **kwargs)
    assert r.status == T.FAILED
    assert "RuntimeError" in r.reason
    assert (r.tier0, r.tier1, r.tier2, r.deviation_mm) == (None, None, None, None)


def test_a_scratch_that_cannot_be_opened_is_a_failure_not_a_crash():
    kwargs, _, _ = harness()
    kwargs["make_scratch"] = lambda *a, **k: (_ for _ in ()).throw(ValueError("no copy"))
    r = T.rehearse(cand(), budget_s=30.0, **kwargs)
    assert (r.status, "ValueError" in r.reason) == (T.FAILED, True)


# --- the ladder ------------------------------------------------------------------------------

@pytest.mark.parametrize("budget_s,tier2", [
    (2.0, False), (10.0, False), (14.0, False), (14.9, False),
    (15.0, True), (15.1, True), (30.0, True), (60.0, True),
])
def test_tier2_runs_only_above_a_quarter_of_its_hard_cap(budget_s, tier2):
    """TABLE, both sides of TIER2_HARD_CAP_S / 4. Below it, tier 2 is None and says no_time."""
    kwargs, _, tiers = harness(statuses=["kept"])
    r = T.rehearse(cand(), budget_s=budget_s, **kwargs)
    assert r.status == T.REHEARSED
    assert (r.tier2 is not None) is tier2
    assert ("2" in [t[0] for t in tiers]) is tier2
    assert ("no_time" in r.reason) is (not tier2)


def test_tier1_is_scoped_and_tier2_is_not():
    kwargs, _, tiers = harness(statuses=["kept"])
    T.rehearse(cand(), budget_s=60.0, **kwargs)
    by_tier = {t[0]: t[1] for t in tiers}
    assert by_tier["1"] is REGION
    assert by_tier["2"] is None


def test_a_refused_rung_is_carried_not_swallowed():
    kwargs, _, _ = harness(statuses=["kept"], tiers={"0": False, "1": None})
    r = T.rehearse(cand(), budget_s=60.0, **kwargs)
    assert (r.tier0.passed, r.tier1.passed, r.tier2.passed) == (False, None, True)
    assert r.status == T.REHEARSED            # rehearsed; `eligible` is what refuses it
    assert T.eligible(r) is False


# --- the defect accounting -------------------------------------------------------------------

def test_cleared_and_revealed_are_scoped_sets():
    kwargs, _, _ = harness(statuses=["kept"], before=("f1", "f2", "outer"),
                           after=("f2", "outer3"))
    r = T.rehearse(cand(), budget_s=60.0, **kwargs)
    # "outer" and "outer3" are outside the grown box, so neither side of the comparison sees them.
    assert r.cleared == frozenset({"f1"})
    assert r.revealed == frozenset()
    assert r.remaining == frozenset({"f2"})


def test_a_revealed_defect_is_in_the_scope_and_is_reported():
    kwargs, _, _ = harness(statuses=["kept"], before=("f1",), after=("f1", "f2"))
    r = T.rehearse(cand(), budget_s=60.0, **kwargs)
    assert (r.cleared, r.revealed, r.remaining) == (frozenset(), frozenset({"f2"}),
                                                    frozenset({"f1"}))
    assert T.eligible(r) is False


# --- one wall, K candidates ------------------------------------------------------------------

@pytest.mark.parametrize("wall_s,shares", [
    (60.0, [20.0, 20.0, 20.0]),
    (30.0, [10.0, 10.0, 10.0]),
    (6.0, [2.0, 2.0, 2.0]),
    # Below the floor for three, the skipped candidate's share goes back to the ones after it
    # rather than being burnt - which is why the second row below rehearses two and the third one.
    (5.99, [None, 2.995, 2.995]),
    (3.0, [None, None, 3.0]),
    (0.0, [None, None, None]),
])
def test_budget_is_split_and_late_candidates_are_not_rehearsed(wall_s, shares):
    """TABLE. Each candidate gets `what is left / how many are still owed one`, capped by the
    wall; a candidate offered less than REHEARSAL_MIN_S is `not_rehearsed` and costs nothing."""
    kwargs, scratches, _ = harness(statuses=["kept"], spend=True)
    cands = [cand(T.RULES, [item()]), cand(T.ONE_SHOT, [item("fill", "f1")]),
             cand(T.INVESTIGATOR, [item("sew", "f2")])]
    out = T.rehearse_all(cands, wall_s=wall_s, limit=3, **kwargs)
    assert len(out) == 3
    expected = [T.NOT_REHEARSED if s is None else T.REHEARSED for s in shares]
    assert [r.status for r in out] == expected
    assert [s.kwargs["remaining_s"] for s in scratches] == pytest.approx(
        [s for s in shares if s is not None])
    for r in out:
        if r.status == T.NOT_REHEARSED:
            assert "no_time" in r.reason and r.kept_ops == 0


@pytest.mark.parametrize("configured,rehearsed", [
    (1, 1), ("1", 1), (2, 2), (3, 3), (None, 1), ("nonsense", 1), (99, 3), (0, 1),
])
def test_k_caps_how_many_are_rehearsed_and_the_rest_say_so(monkeypatch, configured, rehearsed):
    """TABLE over K, including the fail-closed default: K=1 is today's behaviour."""
    if configured is None:
        monkeypatch.delattr(rcfg, "CAD_REPAIR_TOURNAMENT_K", raising=False)
    else:
        monkeypatch.setattr(rcfg, "CAD_REPAIR_TOURNAMENT_K", configured, raising=False)
    kwargs, _, _ = harness(statuses=["kept"])
    cands = [cand(T.RULES, [item()]), cand(T.ONE_SHOT, [item("fill", "f1")]),
             cand(T.INVESTIGATOR, [item("sew", "f2")])]
    out = T.rehearse_all(cands, wall_s=600.0, **kwargs)
    assert [r.status for r in out].count(T.REHEARSED) == rehearsed
    for r in out[rehearsed:]:
        assert r.status == T.NOT_REHEARSED and "K" in r.reason


def test_rehearse_all_never_raises_and_keeps_every_candidate_in_the_record():
    kwargs, _, _ = harness(raises=True)
    cands = [cand(T.RULES, [item()]), cand(T.ONE_SHOT, [item("fill", "f1")])]
    out = T.rehearse_all(cands, wall_s=600.0, limit=2, **kwargs)
    assert [r.status for r in out] == [T.FAILED, T.FAILED]
    assert T.winner(out) is None                     # nothing survived, and it SAYS so


def test_the_session_object_is_never_touched():
    """A spy shape: a rehearsal may read it to copy it, and may never write on it."""

    class Spy:
        def __setattr__(self, name, value):          # pragma: no cover - must never fire
            raise AssertionError(f"a rehearsal wrote {name!r} on the session's shape")

    kwargs, _, _ = harness(statuses=["kept"])
    kwargs["session_shape"] = Spy()
    out = T.rehearse_all([cand(), cand(T.ONE_SHOT, [item("fill", "f1")])], wall_s=600.0, limit=2,
                         **kwargs)
    assert [r.status for r in out] == [T.REHEARSED, T.REHEARSED]


def test_the_winner_of_a_real_tournament_is_the_one_that_cleared_without_revealing():
    kwargs, _, _ = harness(statuses=["kept"])
    good = cand(T.ONE_SHOT, [item()])
    out = [T.rehearse(good, budget_s=60.0, **kwargs)]
    kwargs2, _, _ = harness(statuses=["kept"], before=("f1",), after=("f1", "f2"))
    out.append(T.rehearse(cand(T.RULES, [item("fill", "f1")]), budget_s=60.0, **kwargs2))
    best = T.winner(out)
    assert best is not None and best.candidate.source == T.ONE_SHOT
