# Responsibility: A18 - the expansion stops submitting once a candidate passes every CHEAP member, the candidate
#                 confirmed is the first all-pass IN SUBMISSION ORDER whatever the completion order, and a cheap
#                 all-pass is never mistaken for clean.
# Boundaries: hermetic. `_cheap_all_pass` is tested directly; the submission/reduction order is tested by
#             scrambling how long each candidate takes and asserting the same winner.
from __future__ import annotations

import random
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace as NS

import pytest

from meshpipeline.cad.repair_v4.search import _cheap_all_pass


def _member(status: str = "pass"):
    return NS(name=f"m_{status}", status=status, min_sicn=0.3, error_kind=None)


def _out(kind: str = "ok", statuses=("pass", "pass"), inconclusive: bool = False):
    panel = [_member(s) for s in statuses]
    return NS(kind=kind, verdict=NS(panel=panel, inconclusive=inconclusive))


# --- the condition itself -------------------------------------------------------------------------------------


def test_every_cheap_member_passing_is_the_condition() -> None:
    assert _cheap_all_pass(_out(statuses=("pass",))) is True
    assert _cheap_all_pass(_out(statuses=("pass", "pass", "pass"))) is True
    assert _cheap_all_pass(_out(statuses=("pass", "fail"))) is False
    assert _cheap_all_pass(_out(statuses=("pass", "timeout"))) is False


def test_an_empty_panel_is_not_an_all_pass() -> None:
    """"every member passed" would be vacuously true on no members, and would stop the expansion for nothing."""
    assert _cheap_all_pass(_out(statuses=())) is False


def test_an_inconclusive_cheap_verdict_never_qualifies() -> None:
    """It says nothing about the part, so it is not worth confirming - and `score` raises on it anyway."""
    assert _cheap_all_pass(_out(statuses=("pass", "pass"), inconclusive=True)) is False


def test_a_candidate_that_did_not_apply_never_qualifies() -> None:
    for kind in ("declined", "failed", "error", "validate", "inconclusive"):
        assert _cheap_all_pass(_out(kind=kind)) is False, kind
    assert _cheap_all_pass(NS(kind="ok", verdict=None)) is False


# --- the order that decides, which is the part that must not depend on timing ----------------------------------


def _run_window(statuses_per_candidate, *, window: int, delays: list[float]):
    """The A18 loop in miniature: a fixed submission window, reduction by popping the HEAD, early stop on the first
    all-pass. Returns (the outcomes collected, how many were never submitted)."""
    import time as _time

    to_run = list(range(len(statuses_per_candidate)))

    def evaluate(i: int):
        _time.sleep(delays[i])                     # scrambles COMPLETION order, never submission order
        return _out(statuses=statuses_per_candidate[i]), i

    with ThreadPoolExecutor(max_workers=window) as ex:
        futures: list = []
        collected: list = []
        early, pos = False, 0
        while pos < len(to_run) or futures:
            while not early and pos < len(to_run) and len(futures) < window:
                futures.append(ex.submit(evaluate, to_run[pos]))
                pos += 1
            if not futures:
                break
            out, idx = futures.pop(0).result()
            collected.append((out, idx))
            if not early and _cheap_all_pass(out):
                early = True
    return collected, len(to_run) - pos


def test_the_first_all_pass_in_SUBMISSION_order_wins_however_the_work_finishes() -> None:
    """The constraint LEAD-3 set: never the first to finish. Candidate 3 is the first all-pass; candidate 5 is
    also an all-pass and returns almost instantly, while 3 is slow. 3 must still be the one that stops the
    expansion."""
    statuses = [("pass", "fail")] * 3 + [("pass", "pass")] + [("pass", "fail")] + [("pass", "pass")] * 4
    slow_three = [0.0, 0.0, 0.0, 0.06, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    collected, _ = _run_window(statuses, window=4, delays=slow_three)
    all_pass = [idx for out, idx in collected if _cheap_all_pass(out)]
    assert all_pass[0] == 3, f"the winner must be index 3, got {all_pass}"
    assert [idx for _o, idx in collected] == sorted(idx for _o, idx in collected), "collected in submission order"


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_scrambled_completion_order_confirms_the_SAME_candidate_every_time(seed: int) -> None:
    """The test LEAD-3 asked for: scramble how long each candidate takes and prove the same one is confirmed."""
    rng = random.Random(seed)
    statuses = [("pass", "fail")] * 5 + [("pass", "pass")] * 7
    delays = [rng.uniform(0.0, 0.05) for _ in statuses]
    collected, not_submitted = _run_window(statuses, window=3, delays=delays)
    first = next(idx for out, idx in collected if _cheap_all_pass(out))
    assert first == 5, f"seed {seed}: the first all-pass in submission order is 5, got {first}"
    assert not_submitted > 0, "the early stop must leave some candidates unsubmitted"


def test_the_evaluated_SET_is_a_function_of_the_window_not_of_timing() -> None:
    """With an all-pass at index i and a window of w, the set evaluated is {0 .. i + w - 1}: deterministic. That
    is why the window is fixed rather than 'submit everything and stop when something returns'."""
    statuses = [("pass", "fail")] * 5 + [("pass", "pass")] * 7
    for window in (1, 2, 4):
        seen = set()
        for seed in range(4):
            rng = random.Random(seed)
            collected, _ = _run_window(statuses, window=window,
                                       delays=[rng.uniform(0.0, 0.03) for _ in statuses])
            seen.add(tuple(idx for _o, idx in collected))
        assert len(seen) == 1, f"window {window}: the evaluated set moved with timing: {seen}"
        evaluated = next(iter(seen))
        assert evaluated == tuple(range(min(5 + window, len(statuses)))), (window, evaluated)


def test_with_no_all_pass_every_candidate_still_runs() -> None:
    statuses = [("pass", "fail")] * 6
    collected, not_submitted = _run_window(statuses, window=3, delays=[0.0] * 6)
    assert len(collected) == 6 and not_submitted == 0


# --- the flag (LEAD-3's ruling, 2026-11-24): default OFF, measurable when on --------------------------------------


def test_the_flag_is_OFF_unless_asked_for(monkeypatch) -> None:
    """Default off is the ruling: an unmeasured early stop gambles against the owner's no-loss-of-clean rule."""
    from meshpipeline.cad.repair_v4.search import early_stop_enabled

    monkeypatch.delenv("CRS_V4_EARLY_STOP", raising=False)
    assert early_stop_enabled() is False
    for off in ("", "0", "false", "no", "off", "  ", "nonsense"):
        monkeypatch.setenv("CRS_V4_EARLY_STOP", off)
        assert early_stop_enabled() is False, off
    for on in ("1", "true", "TRUE", "yes", "on", " On "):
        monkeypatch.setenv("CRS_V4_EARLY_STOP", on)
        assert early_stop_enabled() is True, on


def test_the_flag_s_docstring_NAMES_the_two_tests_it_would_break() -> None:
    """LEAD-3's requirement: whoever flips this must read those tests first, so they are named in the docstring
    rather than left to be rediscovered."""
    from meshpipeline.cad.repair_v4.search import early_stop_enabled

    doc = early_stop_enabled.__doc__ or ""
    assert "test_the_search_ai_arm_reaches_the_ai_proposer_and_the_search_det_arm_cannot" in doc
    assert "test_an_inconclusive_child_verdict_does_not_abort_the_branch_and_is_retried_once" in doc
    assert "1.6" in doc and "19.3" in doc, "the measured prize belongs in the docstring"
    assert "DEFAULT OFF" in doc


def test_every_generator_must_be_heard_before_the_stop_may_fire() -> None:
    """Option (1): a det candidate that all-passes must not end an expansion whose AI candidate has not run.
    Modelled on the outcome's `gen` index, as the loop does."""
    contributing = {0, 1}
    heard: set = set()
    fired_at = None
    for i, (gen, all_pass) in enumerate([(0, False), (0, True), (1, True)]):
        heard.add(gen)
        if fired_at is None and heard >= contributing and all_pass:
            fired_at = i
    assert fired_at == 2, "the stop may only fire once generator 1 has been heard from"


def test_with_one_generator_the_first_all_pass_still_stops_it() -> None:
    """The saving survives for search_det, which has a single generator - that is why option (1) is not a no-op."""
    contributing = {0}
    heard: set = set()
    fired_at = None
    for i, (gen, all_pass) in enumerate([(0, False), (0, True), (0, True)]):
        heard.add(gen)
        if fired_at is None and heard >= contributing and all_pass:
            fired_at = i
    assert fired_at == 1
